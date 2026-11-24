from io import BytesIO
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

with patch("dotenv.load_dotenv"):
    from main import ChatMessage, ChatRequest, app, chat, vision_chat


class ChatEndpointConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_accepts_a_non_ark_provider_configuration(self):
        env = {
            "LLM_PROVIDER": "custom",
            "LLM_API_KEY": "custom-secret",
            "LLM_MODEL": "vendor-model",
            "LLM_BASE_URL": "https://llm.example.com/v1",
        }
        request = ChatRequest(messages=[ChatMessage(role="user", content="你好")])

        with patch.dict(os.environ, env, clear=True):
            response = await chat(request)

        self.assertIsInstance(response, StreamingResponse)

    async def test_chat_rejects_an_incomplete_provider_configuration(self):
        request = ChatRequest(messages=[ChatMessage(role="user", content="你好")])

        with patch.dict(
            os.environ,
            {"LLM_PROVIDER": "deepseek", "DEEPSEEK_MODEL": "deepseek-test"},
            clear=True,
        ):
            with self.assertRaises(HTTPException) as context:
                await chat(request)

        self.assertEqual(context.exception.status_code, 500)
        self.assertIn("DEEPSEEK_API_KEY", context.exception.detail)

    async def test_photo_reports_missing_model_configuration(self):
        image = UploadFile(BytesIO(b"test"), headers=Headers({"content-type": "image/png"}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as context:
                await vision_chat(file=image, scan_type="report", user_info="{}", messages="[]", thread_id="")
        self.assertEqual(context.exception.status_code, 500)
        self.assertTrue(context.exception.detail.startswith("大模型配置错误"))


class HealthGatewayTests(unittest.TestCase):
    def test_only_the_gateway_can_start_or_delete_internal_threads(self):
        client = TestClient(app)
        thread = "8" * 32
        env = {"HEALTH_AGENT_TOKEN": "local-test-token"}
        with patch.dict(os.environ, env, clear=True), patch("main._master_app") as graph:
            graph.checkpointer.adelete_thread = AsyncMock()
            self.assertEqual(client.post("/internal/health/chat", json={"messages": []}).status_code, 403)
            self.assertEqual(client.delete(f"/internal/health/threads/{thread}").status_code, 403)
            graph.checkpointer.adelete_thread.assert_not_called()
            headers = {"X-Agent-Token": "local-test-token"}
            self.assertEqual(client.delete(f"/internal/health/threads/{thread}", headers=headers).status_code, 204)
            graph.checkpointer.adelete_thread.assert_awaited_once_with(thread)

    def test_gateway_vision_requires_service_credentials_and_uses_existing_vision_adapter(self):
        client = TestClient(app)
        path = "/internal/health/vision-chat"
        headers = {"X-Agent-Token": "local-test-token"}
        files = {"file": ("synthetic.png", b"test-picture", "image/png")}
        with patch.dict(os.environ, {"HEALTH_AGENT_TOKEN": "local-test-token"}, clear=True), patch(
            "main.vision_chat", new_callable=AsyncMock,
            return_value=StreamingResponse(iter(['data: {"type":"finish"}\n\n']), media_type="text/event-stream"),
        ) as vision:
            self.assertEqual(client.post(path, files=files, data={"scan_type": "drug_box"}).status_code, 403)
            self.assertEqual(client.post(path, files=files, data={"scan_type": "report"}, headers=headers).status_code, 422)
            result = client.post(path, files=files, data={"scan_type": "trace_code"}, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertIn('"type":"finish"', result.text)
        vision.assert_awaited_once()
        self.assertEqual(vision.await_args.args[1], "trace_code")


if __name__ == "__main__":
    unittest.main()
