#!/usr/bin/env python3
"""Execute pinned ZCode option maps and argument parsing without launching the CLI.

This diagnostic never grants native execution readiness. It executes a small,
verified, dependency-free source closure with Node's TypeScript transformer.
It does not load Zod schemas, hooks, credentials, providers, or an agent loop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from xiaolvji.adapters import ZCODE_DISALLOWED_TOOLS, build_command
from xiaolvji.host_config import (
    ZCODE_BUILTIN_PATH, ZCODE_PERSONAL_PATH, ZCODE_SOURCE_COMMIT,
    derive_request_constraints, render_host_config,
)


SOURCE_FILES = tuple(
    "packages/model-option-map/src/" + name + ".ts"
    for name in ("compiler", "evaluator", "index", "merge-patch", "option-maps",
                 "parser", "tokenizer", "types")
) + (
    "apps/zcode-cli/packages/core/src/tool/compat.ts",
    "packages/shared/src/runtimeEnv.ts",
    "apps/zcode-cli/packages/cli/src/arguments.ts",
)


class SourceProbeError(ValueError):
    """A source or execution prerequisite was not established."""


def _clean_environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8",
        "PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(git: str, source: Path, *args: str) -> bytes:
    # No hook, replacement object, ambient credential configuration or lazy
    # network fetch is necessary to read this explicitly supplied checkout.
    with tempfile.TemporaryDirectory(prefix="xiaolvji-zcode-git-") as home:
        result = subprocess.run(
            [git, "--no-replace-objects", "-c", "core.fsmonitor=false",
             "-c", "credential.helper=", "-C", str(source), *args],
            env=_clean_environment(Path(home)), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=15, check=False,
        )
    if result.returncode != 0:
        raise SourceProbeError("Pinned Git source is unavailable locally; complete the checkout first.")
    return result.stdout


def read_pinned_sources(source: Path, git: str) -> dict[str, bytes]:
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise SourceProbeError("The ZCode source must be an existing directory.")
    head = _git(git, source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    if head != ZCODE_SOURCE_COMMIT:
        raise SourceProbeError("The ZCode checkout is not the supported pinned commit.")
    files: dict[str, bytes] = {}
    for name in SOURCE_FILES:
        path = source / name
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise SourceProbeError("Pinned source files are missing; a sparse checkout may be incomplete.") from exc
        if canonical != path or not path.is_file():
            raise SourceProbeError("Pinned source files must be regular files without symlink components.")
        content = path.read_bytes()
        if len(content) > 1_048_576:
            raise SourceProbeError("Pinned source file exceeds the diagnostic input limit.")
        expected = _git(git, source, "cat-file", "blob", ZCODE_SOURCE_COMMIT + ":" + name)
        if content != expected:
            raise SourceProbeError("Pinned source files differ from their committed bytes.")
        files[name] = content
    return files


def build_cases() -> list[dict]:
    """Build credential-free cases from the actual production renderer."""
    definitions = [
        ("responses-high", "openai-responses", {"reasoningLevel": "high", "maxOutputTokens": 32_000}),
        ("responses-ultra", "openai-responses", {"reasoningLevel": "ultra", "maxOutputTokens": 2**31 - 1}),
        ("chat-high", "openai-chat-completions", {"reasoningLevel": "high", "maxOutputTokens": 32_000}),
        ("chat-ultra", "openai-chat-completions", {"reasoningLevel": "ultra", "maxOutputTokens": 64_000}),
        ("anthropic-enabled", "anthropic-messages", {"reasoningLevel": "high", "maxOutputTokens": 32_000,
                                                    "thinking": {"type": "enabled", "budget_tokens": 8_000}}),
        ("anthropic-disabled", "anthropic-messages", {"reasoningLevel": "high", "maxOutputTokens": 32_000,
                                                     "thinking": {"type": "disabled"}}),
        ("anthropic-adaptive", "anthropic-messages", {"reasoningLevel": "ultra", "maxOutputTokens": 32_000,
                                                     "thinking": {"type": "adaptive"},
                                                     "output_config": {"effort": "high"}}),
    ]
    cases = []
    for name, api, quality in definitions:
        snapshot = {
            "schema_version": 1, "engine": "zcode", "provider_id": "source-probe",
            "api": api, "model": "owner/model.v1+revision:exact", "quality": quality,
            "zcode_model_properties": {
                "requiresMfjsToolSchema": False, "contextWindow": 200_000,
                "inputFormat": {"supportsText": True, "supportsImage": False,
                                "supportsVideo": False, "supportsAudio": False, "supportsPdf": False},
                "outputFormat": {"supportsText": True}, "supportsToolCall": True,
                "supportsJsonSchemaOutput": False, "supportsNativeWebSearch": False,
                "supportsMidConversationSystem": False,
            },
        }
        rendered = render_host_config(snapshot, "http://127.0.0.1:38119", "source_probe_" + "A" * 48)
        files = {item["relative_path"]: json.loads(item["content"]) for item in rendered["files"]}
        builtin = files[ZCODE_BUILTIN_PATH]["config"]["modelConfigRules"]["modelRules"][0]
        selection = files[ZCODE_PERSONAL_PATH]["config"]["defaultModelSelection"]
        cases.append({"name": name, "api": api, "model": snapshot["model"],
                      "rule": builtin, "selection": selection, "env": rendered["env"],
                      "properties": snapshot["zcode_model_properties"],
                      "expected": derive_request_constraints(snapshot)})
    return cases


def build_argument_cases(workspace: Path) -> list[dict]:
    """Exercise the production argv builder with synthetic, lossless prompts."""
    cwd = str(workspace.resolve())
    max_characters = 28_000 - len(cwd) - len("zcode") if os.name == "nt" else 120_000
    definitions = [
        ("ordinary", "Implement one independent module."),
        ("unicode-multiline", '修复独立模块\nKeep "quotes" and spaces; $(not a command).'),
        ("leading-option", "--help"),
        ("tool-rule-text", "--disallowed-tools=Read"),
        ("mode-option-text", "--mode=unrestricted"),
        ("near-ascii-limit", "x" * min(120_000, max_characters)),
        ("near-utf8-limit", "绿" * min(40_000, max_characters)),
    ]
    cases = []
    for name, prompt in definitions:
        command = build_command("zcode", workspace, prompt)
        cases.append({"name": name, "argv": command["argv"][1:],
                      "expected": {"cwd": cwd, "prompt": prompt,
                                   "output-format": "stream-json",
                                   "workflow-mode": "disabled", "mode": "guarded"}})
    return cases


NODE_HARNESS = r"""
import assert from 'node:assert/strict';
import {writeFile,mkdir} from 'node:fs/promises';
import {readFileSync} from 'node:fs';
import {dirname,join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {stripTypeScriptTypes} from 'node:module';
const input=JSON.parse(readFileSync(0,'utf8'));
const output=process.argv[1];
for(const [relative,source] of Object.entries(input.sources)){
  const target=join(output,relative.replace(/\.ts$/,'.js'));
  await mkdir(dirname(target),{recursive:true});
  await writeFile(target,stripTypeScriptTypes(source,{mode:'transform',sourceMap:false}));
}
await writeFile(join(output,'package.json'),JSON.stringify({type:'module'}));
const native=await import(pathToFileURL(join(output,'packages/model-option-map/src/index.js')));
const compat=await import(pathToFileURL(join(output,'apps/zcode-cli/packages/core/src/tool/compat.js')));
const runtime=await import(pathToFileURL(join(output,'packages/shared/src/runtimeEnv.js')));
const nativeArguments=await import(pathToFileURL(join(output,'apps/zcode-cli/packages/cli/src/arguments.js')));
const cases=[];
for(const item of input.cases){
  const maps=native.compileModelOptionMaps(item.rule.config.optionSpecs);
  const actual=maps.apply({model:item.model},item.selection.options);
  // The native merge-patch implementation deliberately uses null prototypes.
  // Compare its JSON wire value, preserving numeric and Boolean types.
  assert.deepStrictEqual(JSON.parse(JSON.stringify(actual)),item.expected,item.name+' quality mapping');
  assert.equal(item.selection.modelId,item.model);
  assert.deepStrictEqual(item.rule.config.properties,item.properties);
  assert.deepStrictEqual(item.rule.config.optionSpecs.reasoningLevel.values,[item.selection.options.reasoningLevel]);
  assert.equal(item.rule.config.optionSpecs.maxOutputTokens.max,item.selection.options.maxOutputTokens);
  const pattern=new RegExp(item.rule.modelMatch);
  assert.ok(pattern.test(item.model));
  assert.ok(!pattern.test('prefix-'+item.model));
  assert.ok(!pattern.test(item.model+'-suffix'));
  assert.ok(!pattern.test(item.model.replace('.','X').replace('+','11')));
  const env=runtime.sanitizeZCodeRuntimeEnv({...item.env,HTTP_PROXY:'http://invalid.test:9',HTTPS_PROXY:'http://invalid.test:9'});
  for(const [key,value] of Object.entries(item.env)) assert.equal(env[key],value);
  assert.equal(env.HTTP_PROXY,undefined);
  assert.equal(env.HTTPS_PROXY,undefined);
  cases.push({name:item.name,api:item.api,passed:true});
}
for(const name of ['Agent','Task']){
  assert.equal(compat.isSubagentDispatchToolName(name),true);
  assert.ok(input.disallowed_tools.includes(name));
}
assert.equal(compat.isSubagentDispatchToolName('TaskOutput'),false);
assert.throws(()=>native.compileModelOptionMap('{"reasoning":{"effort":unknownVariable}}','reasoningLevel'));
assert.throws(()=>native.compileModelOptionMap('{','maxOutputTokens'));
assert.throws(()=>native.compileModelOptionMaps(input.cases[0].rule.config.optionSpecs).apply({}, {maxOutputTokens:32000}));
const argumentCases=[];
let argumentNegativeControls=0;
for(const item of input.argument_cases){
  // This is the same order used by the pinned CLI's run.ts. The input argv is
  // generated by Xiaolvji's production adapter, not a replacement CLI parser.
  const extracted=nativeArguments.extractDisallowedToolsArgs(item.argv);
  const parsed=nativeArguments.parseGlobalArgs(extracted.args);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(parsed.values)),item.expected,item.name+' argument values');
  assert.deepStrictEqual(parsed.positionals,[],item.name+' no positional command');
  assert.deepStrictEqual(extracted.toolDisallowlist,input.disallowed_tools,item.name+' denied tools');
  assert.equal(nativeArguments.isProtocolServerInvocation(extracted.args),false);
  if(['leading-option','tool-rule-text','mode-option-text'].includes(item.name)){
    // A split option/value reproduces the former bug in the real native parser.
    const split=item.argv.flatMap(arg=>arg.startsWith('--prompt=') ? ['--prompt',arg.slice(9)] : [arg]);
    assert.throws(()=>nativeArguments.parseGlobalArgs(nativeArguments.extractDisallowedToolsArgs(split).args),
                  {code:'ERR_PARSE_ARGS_INVALID_OPTION_VALUE'});
    argumentNegativeControls+=1;
  }
  argumentCases.push({name:item.name,prompt_bytes:Buffer.byteLength(item.expected.prompt,'utf8'),passed:true});
}
process.stdout.write(JSON.stringify({cases,negative_controls:3,alias_controls:3,environment_controls:2*cases.length,
  argument_cases:argumentCases,argument_negative_controls:argumentNegativeControls,argument_parser_tested:true}));
"""


def probe_source(source: Path, node: str | None = None, git: str | None = None) -> dict:
    git = git or shutil.which("git")
    node = node or shutil.which("node")
    if not git or not node:
        raise SourceProbeError("Both Git and Node.js are required for the source diagnostic.")
    sources = read_pinned_sources(source, git)
    with tempfile.TemporaryDirectory(prefix="xiaolvji-zcode-source-") as temp:
        directory = Path(temp)
        env = _clean_environment(directory)
        version_result = subprocess.run([node, "--version"], env=env, text=True,
                                        capture_output=True, timeout=10, check=False)
        version = version_result.stdout.strip()
        if version_result.returncode or not re.fullmatch(r"v24\.(\d+)\.(\d+)", version):
            raise SourceProbeError("This diagnostic requires Node.js 24 with the built-in TypeScript transformer.")
        if int(version.split(".")[1]) < 14:
            raise SourceProbeError("This diagnostic requires Node.js 24.14 or later in the 24.x series.")
        workspace = directory / "worker path 数据"
        workspace.mkdir()
        payload = {"sources": {path: content.decode("utf-8") for path, content in sources.items()},
                   "cases": build_cases(), "disallowed_tools": list(ZCODE_DISALLOWED_TOOLS),
                   "argument_cases": build_argument_cases(workspace)}
        result = subprocess.run(
            [node, "--input-type=module", "-e", NODE_HARNESS, str(directory)],
            input=json.dumps(payload), env=env, text=True, capture_output=True,
            timeout=30, check=False,
        )
    if result.returncode != 0:
        raise SourceProbeError("The pinned native option-map, alias, environment, or argument checks failed.")
    evidence = json.loads(result.stdout)
    return {
        "schema_version": 1, "status": "passed", "evidence_level": "native_mapping_only",
        "source_commit": ZCODE_SOURCE_COMMIT, "node_version": version,
        "documented_build_node_version": "v24.14.0",
        "source_files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                         for name, content in sources.items()],
        **evidence,
        "configuration_schema_loaded": False, "cli_launched": False,
        "real_model_request_tested": False, "native_execution_ready": False,
        "not_verified": ["Complete Zod provider schema loading", "CLI/provider streaming and model turn",
                         "Managed policy, hooks, trust and live tool inventory",
                         "Native cancellation and strict sandbox compatibility"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Complete pinned ZCode source checkout; never fetched automatically.")
    parser.add_argument("--node", help="Explicit Node.js 24 executable (default: node on PATH).")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = probe_source(args.source, args.node)
    except (SourceProbeError, OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError) as exc:
        report = {"schema_version": 1, "status": "blocked", "evidence_level": "native_mapping_only",
                  "source_commit": ZCODE_SOURCE_COMMIT, "argument_parser_tested": False,
                  "configuration_schema_loaded": False, "cli_launched": False,
                  "real_model_request_tested": False, "native_execution_ready": False,
                  "reason": str(exc) if isinstance(exc, SourceProbeError) else "Source diagnostic prerequisites or execution failed."}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "evidence_level": report["evidence_level"],
                      "native_execution_ready": False, "report": str(args.report)}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
