import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.vision import (
    VisionInputError,
    build_vision_prompt,
    compose_agent_message,
    normalize_scan_type,
    parse_report_items,
    recognize_image,
    redact_sensitive_text,
    validate_image_upload,
)


class VisionAdapterTests(unittest.TestCase):
    def test_normalize_scan_type_accepts_supported_values(self):
        self.assertEqual(normalize_scan_type(" report "), "report")
        self.assertEqual(normalize_scan_type("DRUG_BOX"), "drug_box")
        self.assertEqual(normalize_scan_type("trace_code"), "trace_code")

    def test_normalize_scan_type_rejects_unknown_value(self):
        with self.assertRaises(VisionInputError):
            normalize_scan_type("avatar")

    def test_validate_image_upload_rejects_unsafe_inputs(self):
        with self.assertRaises(VisionInputError):
            validate_image_upload("application/pdf", 1024)
        with self.assertRaises(VisionInputError):
            validate_image_upload("image/png", 0)
        with self.assertRaises(VisionInputError):
            validate_image_upload("image/png", 8 * 1024 * 1024 + 1)

    def test_report_prompt_forbids_personal_identity_output(self):
        prompt = build_vision_prompt("report")

        self.assertIn("不要提取或输出姓名", prompt)
        self.assertIn("身份证号", prompt)
        self.assertIn("已隐藏", prompt)
        self.assertIn("不要诊断疾病", prompt)

    def test_redact_sensitive_text_removes_common_pii(self):
        raw = "姓名：张三\n手机号：13800138000\n身份证号：110105199001011234\n白细胞：11.2"

        redacted = redact_sensitive_text(raw)

        self.assertIn("姓名：已隐藏", redacted)
        self.assertIn("手机号：已隐藏", redacted)
        self.assertIn("身份证号：已隐藏", redacted)
        self.assertIn("白细胞：11.2", redacted)
        self.assertNotIn("张三", redacted)
        self.assertNotIn("13800138000", redacted)
        self.assertNotIn("110105199001011234", redacted)

    def test_compose_report_message_marks_identity_hidden(self):
        message = compose_agent_message("report", "姓名：李四\n白细胞：偏高")

        self.assertIn("个人身份信息已隐藏", message)
        self.assertIn("姓名：已隐藏", message)
        self.assertIn("请基于以上内容进行报告解读", message)

    def test_report_ocr_retains_ranges_and_skips_non_numeric_rows(self):
        items = parse_report_items(
            '```json\n{"items": ['
            '{"name":"示例一","value":"4.2","unit":"U/L","reference_range":"3–10"},'
            '{"name":"示例二","value":4,"reference_range":null},'
            '{"name":"示例三","value":"无法确认","reference_range":"1–2"}]}\n```'
        )
        self.assertEqual([(item.value, item.reference_range) for item in items], [(4.2, "3–10"), (4, None)])
        array_items = parse_report_items('```json\n[{"name":"例一","value":2,"reference_range":"1–3"}]\n```')
        self.assertEqual([item.reference_range for item in array_items], ["1–3"])
        self.assertEqual(parse_report_items("没有可辨识的结果"), [])

    def test_compose_drug_box_message_routes_to_pharmacy_context(self):
        message = compose_agent_message("drug_box", "药品名称：布洛芬缓释胶囊")

        self.assertIn("用户上传了一张药盒图片", message)
        self.assertIn("禁忌", message)

    def test_agent_message_uses_a_provider_neutral_vision_label(self):
        message = compose_agent_message("report", "白细胞：11.2")

        self.assertIn("视觉模型识别结果", message)
        self.assertNotIn("火山视觉模型", message)

    def test_recognize_image_uses_the_selected_compatible_provider(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="药品名称：布洛芬"))]
        )
        cases = (
            ("custom", "https://llm.example.com/v1", "vision-model", None),
            ("deepseek", "https://api.deepseek.com", "deepseek-flash", {"thinking": {"type": "disabled"}}),
        )
        for provider, base_url, model, extra_body in cases:
            with self.subTest(provider=provider):
                create = AsyncMock(return_value=response)
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                env = {"LLM_PROVIDER": provider, "LLM_API_KEY": "test-secret", "LLM_MODEL": model,
                       "LLM_BASE_URL": base_url, "VISION_MODEL": model}
                with (patch.dict(os.environ, env, clear=True),
                      patch("openai.AsyncOpenAI", return_value=client) as client_class):
                    result = asyncio.run(recognize_image(b"valid-image", "image/jpeg", "drug_box"))
                client_class.assert_called_once_with(api_key="test-secret", base_url=base_url)
                request = create.await_args.kwargs
                self.assertEqual(request["model"], model)
                self.assertEqual(request.get("extra_body"), extra_body)
                self.assertEqual(request["messages"][0]["content"][1]["type"], "image_url")
                self.assertEqual(result, "药品名称：布洛芬")


if __name__ == "__main__":
    unittest.main()
