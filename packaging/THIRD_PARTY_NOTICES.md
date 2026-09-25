# Third-party notices

This release contains application/runtime software but no model weights. Release
publishers must update the version/revision fields below to match the exact files
used for a build and must retain any additional notices shipped with native DLLs.

## stable-diffusion.cpp

- Project: <https://github.com/leejet/stable-diffusion.cpp>
- Release revision: recorded by the release publisher
- License: MIT

The authoritative upstream copyright line and license file shipped with the
selected native runtime must remain in the `runtime/` directory. Do not publish
a runtime directory that omits those original notices.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

CUDA, cuBLAS, and other NVIDIA runtime files are subject to NVIDIA's applicable
redistribution terms. A publisher must verify that every copied DLL is
redistributable and include the notices delivered with the selected CUDA build.

## Pillow 12.3.0

- Project: <https://python-pillow.github.io/>
- License: HPND
- License text: <https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE>

The bundled wheel is `pillow-12.3.0-cp312-cp312-win_amd64.whl`. Its license and
copyright notices remain available from the linked upstream release.

## pystray 0.19.5

- Project: <https://github.com/moses-palmer/pystray>
- License: GNU Lesser General Public License v3.0
- License text: <https://github.com/moses-palmer/pystray/blob/v0.19.5/COPYING.LGPL>

The Windows tray controller bundles pystray through PyInstaller. Release
publishers must retain its license notice and satisfy the LGPL requirements for
the exact bundled version.

## six 1.17.0

- Project: <https://github.com/benjaminp/six>
- License: MIT
- License text: <https://github.com/benjaminp/six/blob/1.17.0/LICENSE>

Six is a runtime dependency of pystray and is included in the application
bundle.

## keyring 25.6.0

- Project: <https://github.com/jaraco/keyring>
- License: MIT
- License text: <https://github.com/jaraco/keyring/blob/v25.6.0/LICENSE>

The Windows build uses keyring's WinVault backend to keep optional Hugging Face
tokens in Windows Credential Manager. No plaintext credential fallback is used.

## Python 3.12

- Project: <https://www.python.org/>
- License: Python Software Foundation License Version 2
- License text: <https://docs.python.org/3.12/license.html>

PyInstaller onedir builds include the Python runtime and standard library. The
PSF license permits redistribution subject to its notice requirements.

## PyInstaller bootloader

- Project: <https://pyinstaller.org/>
- Build dependency version: pinned in `requirements-build.txt`
- License: GPL-2.0-or-later with the PyInstaller Bootloader Exception
- License details: <https://pyinstaller.org/en/stable/license.html>

The exception permits distributing the generated application under the
application's own license.

## Qwen-Image-2.1 model files

Model files are deliberately excluded from the installer and portable archive.
The model manager downloads only release-manifest entries after explicit license
acceptance and SHA-256 verification. The current license finding is the Qwen
Research License Agreement with non-commercial-use restrictions unless a
separate commercial license is obtained. See `QWEN_RESEARCH_LICENSE_NOTICE.txt`
and the official license at the pinned model source. This is not legal advice.

## Qwen3-VL-8B-Instruct GGUF companion files

The text encoder and vision projector are downloaded from
`Qwen/Qwen3-VL-8B-Instruct-GGUF` at the immutable revision recorded in the
release manifest. The upstream repository declares Apache License 2.0. These
files are not included in the installer or portable archive.
