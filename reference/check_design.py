#!/usr/bin/env python3
"""Bounded static checks for the design package, not a JSON Schema validator.

This checks JSON syntax, selected cross-file invariants, local schema references,
and the English-only instruction sources. It never starts either agent host.
"""
from pathlib import Path
import json
import re
import sys

BASE = Path(__file__).resolve().parent.parent


def reject_constant(value):
    raise ValueError(f"Nonstandard JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse(path):
    return json.loads(path.read_text(encoding="utf-8"),
                      object_pairs_hook=unique_object, parse_constant=reject_constant)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check():
    checks = []
    paths = sorted(p for p in BASE.rglob("*") if p.is_file()
                   and (p.name.endswith(".json") or p.name.endswith(".json.example")))
    objects = {str(p.relative_to(BASE)): parse(p) for p in paths}
    checks.append({"check": "strict_json_parse", "files": len(paths), "status": "passed"})

    english = sorted(p for p in (BASE / "spec").rglob("*") if p.is_file())
    for p in english:
        require(not re.search(r"[\u3400-\u9fff]", p.read_text(encoding="utf-8")),
                f"Unexpected CJK text in instruction/template source: {p.name}")
    checks.append({"check": "instruction_source_cjk_scan", "files": len(english),
                   "status": "passed", "note": "Language was also reviewed; this scan alone cannot prove English semantics."})

    policy = (BASE / "spec/AGENTS.block.en.md").read_text(encoding="utf-8")
    require(policy.count("parallel-development:managed-policy:begin") == 1
            and policy.count("parallel-development:managed-policy:end") == 1,
            "Managed policy must have exactly one marker pair.")
    skill = (BASE / "spec/parallel-development.SKILL.md").read_text(encoding="utf-8")
    require(skill.startswith("---\nname: parallel-development\ndescription: ")
            and "\n---\n" in skill, "Skill metadata is incomplete.")
    require("Do not downgrade the selected profile" in skill,
            "The Skill must forbid unauthorized profile downgrade.")
    checks.append({"check": "policy_markers_and_skill_metadata", "status": "passed"})

    codex = objects["spec/codex/hooks.json.example"]["hooks"]
    require(set(codex) == {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse",
                            "Stop", "SubagentStop", "Interrupt"}, "Unexpected Codex event set.")
    for event, groups in codex.items():
        for group in groups:
            for handler in group["hooks"]:
                require(handler["type"] == "command" and handler["async"] is False,
                        "Codex admission handlers must be synchronous commands.")
                require(handler["timeout"] == (3 if event == "Interrupt" else 10),
                        "Unexpected Codex timeout budget.")
                require("timeoutMs" not in handler and "commandWindows" in handler,
                        "Mixed Codex/ZCode handler fields.")
    modern = objects["spec/codex/plugin.json.example"]
    compat = objects["spec/codex/plugin.compat.json.example"]
    require(modern["name"] == compat["name"] and modern["version"] == compat["version"],
            "Codex manifest identities differ.")
    require(modern["extensions"]["com.openai"]["hooks"] == compat["hooks"],
            "Codex hook paths differ.")
    checks.append({"check": "codex_template_consistency", "status": "passed"})

    zcode = objects["spec/zcode/hooks.json.example"]["hooks"]
    windows = objects["spec/zcode/hooks.windows.json.example"]["hooks"]
    events = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}
    require(set(zcode) == set(windows) == events, "Unexpected ZCode event set.")
    require("hooks" not in objects["spec/zcode/plugin.json.example"],
            "Do not duplicate standard ZCode hook discovery.")
    for event in events:
        for a, b in zip(zcode[event], windows[event], strict=True):
            for posix, win in zip(a["hooks"], b["hooks"], strict=True):
                require(posix["type"] == win["type"] == "process", "Expected process handlers.")
                require(posix["args"] == win["args"] == ["hook", "--adapter", "zcode"],
                        "Unexpected ZCode handler arguments.")
                require(win["command"] == posix["command"] + ".exe", "Unexpected platform paths.")
                require(posix["timeoutMs"] == win["timeoutMs"] == 10000, "Unexpected timeoutMs.")
                require("commandWindows" not in posix and "timeout" not in posix,
                        "Mixed host configuration fields.")
                require(posix.get("onError") == ("deny" if event == "PreToolUse" else None),
                        "onError policy belongs only on the verified PreToolUse path.")
    checks.append({"check": "zcode_template_consistency", "status": "passed"})

    refs = patterns = 0
    for filename, schema in objects.items():
        if not filename.startswith("schemas/"):
            continue
        require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
                "Unexpected JSON Schema draft.")
        for node in walk(schema):
            if "$ref" in node:
                ref = node["$ref"]
                require(ref.startswith("#/"), "This check only accepts local schema references.")
                target = schema
                for part in ref[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
                refs += 1
            if "pattern" in node:
                re.compile(node["pattern"])
                patterns += 1
    require(objects["schemas/task.schema.json"]["properties"]["retry"]["properties"]
            ["max_retries"]["maximum"] == 2, "Retry policy and schema disagree.")
    require(objects["examples/project-config.json"]["enabled"] is False,
            "The example must not pretend an installed runtime is active.")
    checks.append({"check": "schema_local_references_and_selected_invariants", "references": refs,
                   "patterns": patterns, "status": "passed"})

    hook_doc = (BASE / "spec/hook-contracts.en.md").read_text(encoding="utf-8")
    samples = re.findall(r"```json\n(.*?)\n```", hook_doc, re.S)
    for sample in samples:
        json.loads(sample, object_pairs_hook=unique_object, parse_constant=reject_constant)
    checks.append({"check": "hook_response_example_json", "examples": len(samples), "status": "passed"})
    return {"status": "passed_with_declared_scope", "checks": checks,
            "not_performed": ["Complete Draft 2020-12 schema validation", "Host plugin loading",
                              "Hook execution or enforcement", "Scheduler concurrency tests",
                              "OS sandbox or cross-platform acceptance"]}


if __name__ == "__main__":
    try:
        print(json.dumps(check(), indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
