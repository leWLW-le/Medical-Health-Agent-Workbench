package dev.reportaudit;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.NullNode;
import com.fasterxml.jackson.databind.node.TextNode;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;

final class ReportChanges {
    record Change(String key, String code, String field, JsonNode before, JsonNode after) {}

    private ReportChanges() {}

    static List<Change> between(JsonNode before, JsonNode after) {
        var oldFindings = findings(before);
        var newFindings = findings(after);
        var changes = new ArrayList<Change>();
        String oldSummary = summary(oldFindings);
        String newSummary = summary(newFindings);
        if (!oldSummary.equals(newSummary)) {
            changes.add(new Change("summary", null, "summary", TextNode.valueOf(oldSummary), TextNode.valueOf(newSummary)));
        }
        var codes = new TreeSet<>(oldFindings.keySet());
        codes.addAll(newFindings.keySet());
        for (String code : codes) {
            JsonNode oldFinding = oldFindings.get(code);
            JsonNode newFinding = newFindings.get(code);
            if (oldFinding == null || newFinding == null) {
                changes.add(change(code, "presence", reportRow(oldFinding), reportRow(newFinding)));
                continue;
            }
            compare(changes, code, "value", oldFinding.path("row").path("value"), newFinding.path("row").path("value"));
            compare(changes, code, "unit", oldFinding.path("row").path("unit"), newFinding.path("row").path("unit"));
            compare(changes, code, "collected_on", oldFinding.path("row").path("collected_on"), newFinding.path("row").path("collected_on"));
            compare(changes, code, "evidence", oldFinding.path("evidence"), newFinding.path("evidence"));
            compare(changes, code, "issues", oldFinding.path("issues"), newFinding.path("issues"));
        }
        return changes;
    }

    private static Map<String, JsonNode> findings(JsonNode snapshot) {
        var findings = new TreeMap<String, JsonNode>();
        snapshot.path("findings").forEach(finding -> findings.put(finding.path("row").path("code").asText(), finding));
        return findings;
    }

    private static String summary(Map<String, JsonNode> findings) {
        var problemCodes = findings.entrySet().stream().filter(item -> !item.getValue().path("issues").isEmpty())
            .map(Map.Entry::getKey).toList();
        return findings.size() + " 项 · " + (problemCodes.isEmpty() ? "无待核对项目" : "待核对 " + String.join("、", problemCodes));
    }

    private static JsonNode reportRow(JsonNode finding) {
        return finding == null ? NullNode.getInstance() : finding.path("row");
    }

    private static void compare(List<Change> changes, String code, String field, JsonNode before, JsonNode after) {
        if (before.equals(after)) return;
        if (field.equals("value") && new BigDecimal(before.asText()).compareTo(new BigDecimal(after.asText())) == 0) return;
        changes.add(change(code, field, before, after));
    }

    private static Change change(String code, String field, JsonNode before, JsonNode after) {
        return new Change(code + "." + field, code, field, before, after);
    }
}
