from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MaskUiTests(unittest.TestCase):
    def test_mask_editor_controls_and_pointer_state_are_present(self) -> None:
        html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        for ident in (
            "maskCanvas", "maskBrush", "maskErase", "maskSize", "maskFeather",
            "maskUndo", "maskRedo", "maskClear", "maskInvert", "modelState", "modelPid",
            "modelsDialog", "modelCatalog", "downloadProgress", "downloadFiles", "idleTimeout",
            "setupBanner", "downloadCancel", "downloadRetry",
            "sourcePanel", "hfRepo", "hfRevision", "hfSourceFiles", "hfSourceResolve",
            "hfSourceConfirm", "hfSourceReset", "hfToken", "hfTokenSet", "hfTokenTest", "hfTokenRemove",
            "customSourceAck", "sourceTrustWarning", "downloadTrustWarning", "modelTrustWarning",
        ):
            self.assertIn(f'id="{ident}"', html)
        self.assertIn('addEventListener("pointerdown"', script)
        self.assertIn('addEventListener("pointermove"', script)
        self.assertIn("maskState.undo", script)
        self.assertIn("maskState.redo", script)
        self.assertIn('q("/api/mask")', script)
        self.assertIn('type === "model"', script)
        self.assertIn('type === "models"', script)
        self.assertIn('modelAction("/api/models/install"', script)
        self.assertIn('modelAction("/api/models/delete"', script)
        self.assertIn('api("/api/runtime"', script)
        self.assertIn('api("/api/models/source/resolve"', script)
        self.assertIn('tokenAction("/api/models/hf-token/set"', script)
        self.assertIn("UNVERIFIED", html)
        self.assertIn('profile.unverified ? el("span"', script)
        self.assertIn("transfer.indeterminate", script)
        self.assertIn("responsibility_acknowledged", script)


if __name__ == "__main__":
    unittest.main()
