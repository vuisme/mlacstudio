from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_bootstrap_is_compiled_tiny_online_installer_without_payloads(self) -> None:
        source = (ROOT / "packaging" / "bootstrap" / "MLACStudioBootstrap.cs").read_text(encoding="utf-8")
        build = (ROOT / "packaging" / "build-bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("/target:winexe", build)
        self.assertIn("MLAC-Studio-Setup-$Version.exe", build)
        self.assertNotIn("python", source.lower())
        self.assertNotIn("cuda", source.lower())
        self.assertNotIn("sd-server", source.lower())
        self.assertNotIn("nvidia-runtime", source.lower())
        self.assertIn("Model weights are forbidden", source)
        self.assertIn("VerifyEnvelope", source)
        self.assertIn("AddRange", source)
        self.assertIn("activation-journal.json", source)
        self.assertIn("ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12", source)
        self.assertLess(
            source.index("ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12"),
            source.index("DownloadText(manifestUrl)"),
        )
        public_key = json.loads((ROOT / "packaging" / "keys" / "mlac-update-public.json").read_text(encoding="utf-8"))
        expected_xml = (
            "<RSAKeyValue><Modulus>"
            + public_key["modulus"]
            + "</Modulus><Exponent>"
            + public_key["exponent"]
            + "</Exponent></RSAKeyValue>"
        )
        self.assertIn(f'private const string KeyId = "{public_key["key_id"]}";', source)
        self.assertIn(f'private const string PublicKeyXml = "{expected_xml}";', source)

    def test_core_bundle_excludes_native_runtime_and_embeds_update_policy(self) -> None:
        spec = (ROOT / "packaging" / "mlac-studio.spec").read_text(encoding="utf-8")
        build = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn('name="MLACStudio"', spec)
        self.assertIn('"model-manager.py"', spec)
        self.assertIn('"updater.py"', spec)
        self.assertIn("mlac-update-public.json", spec)
        self.assertIn("binaries=[]", spec)
        self.assertIn("Core component must not contain native inference runtimes", build)
        self.assertNotIn("RuntimeDirectory", build)

    def test_component_release_is_signed_deterministic_and_never_contains_models(self) -> None:
        tools = (ROOT / "packaging" / "release-tools.py").read_text(encoding="utf-8")
        components = (ROOT / "packaging" / "build-components.ps1").read_text(encoding="utf-8")
        github = (ROOT / "packaging" / "github-release.ps1").read_text(encoding="utf-8")
        self.assertIn("FIXED_ZIP_TIME", tools)
        self.assertIn("MODEL_SUFFIXES", tools)
        self.assertIn("sign_payload", tools)
        self.assertIn("core=core=", components)
        self.assertNotIn("common-runtime", components)
        self.assertNotIn("nvidia-runtime", components)
        self.assertNotIn("RuntimeDirectory", components)
        self.assertIn("I APPROVE MLAC RELEASE", github)

    def test_github_release_build_uses_pinned_on_demand_runtime_source(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release-draft.yml").read_text(encoding="utf-8")
        template = json.loads((ROOT / "packaging" / "release-manifest.example.json").read_text(encoding="utf-8"))
        runtime = next(item for item in template["artifacts"] if item["root"] == "runtime")
        self.assertNotIn("MLAC_RUNTIME_DIRECTORY", workflow)
        self.assertIn("MLAC_RUNTIME_URL", workflow)
        self.assertIn("MLAC_RUNTIME_SIZE", workflow)
        self.assertIn("MLAC_RUNTIME_SHA256", workflow)
        self.assertEqual(runtime["delivery"], "download")
        self.assertEqual(runtime["archive"]["format"], "zip")
        self.assertEqual(set(runtime["archive"]["members"]), {"sd_cli", "sd_server"})

    def test_packaged_hf_sources_are_exact_and_release_build_validates_them(self) -> None:
        template = json.loads((ROOT / "packaging" / "release-manifest.example.json").read_text(encoding="utf-8"))
        urls = json.loads((ROOT / "packaging" / "artifact-urls.json").read_text(encoding="utf-8"))
        artifacts = {item["id"]: item for item in template["artifacts"]}
        expected = {
            "transformer-q4-0": ("qwen-image-2.1-UC-Q4_0.gguf", 4151573280, "13f59f20656efc0aa385d03c1fcac1a9dc2ad6e5ccc0ea9bfe1d6ac636f2c5b9"),
            "transformer-q4-k-m": ("qwen-image-2.1-UC-Q4_K_M.gguf", 4604558112, "e79c8a009f2ecbdb6c70fd663d9aea9ee304a0d91f347e4169a756b8ad141b41"),
            "transformer-q5-k-m": ("qwen-image-2.1-UC-Q5_K_M.gguf", 5221284640, "af0bf278cf16d204fb31c384dc82fd41dca82d976b15fe9305a60c726fd6f821"),
        }
        for artifact_id, (name, size, digest) in expected.items():
            self.assertEqual(Path(artifacts[artifact_id]["path"]).name, name)
            self.assertEqual(artifacts[artifact_id]["size"], size)
            self.assertEqual(artifacts[artifact_id]["sha256"], digest)
            self.assertIn(f"/resolve/40319fb15542f0ad22921e0124a191a8a935a60a/{name}", urls[artifact_id])
        companion_revision = "/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/f982a07559d4a2f6c8744d840bf6fccab30eea96/"
        self.assertIn(companion_revision + "Qwen3VL-8B-Instruct-Q4_K_M.gguf", urls["text-encoder-q4-k-m"])
        self.assertIn(companion_revision + "mmproj-Qwen3VL-8B-Instruct-F16.gguf", urls["vision-projector-f16"])
        self.assertEqual(artifacts["text-encoder-q4-k-m"]["license"], "qwen3-vl-apache-2.0")
        self.assertEqual(artifacts["vision-projector-f16"]["license"], "qwen3-vl-apache-2.0")
        build = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("validate-release-sources.py", build)


if __name__ == "__main__":
    unittest.main()
