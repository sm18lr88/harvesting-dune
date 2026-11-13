# Development

```powershell
uv sync          # dev tools, including pytest
uv run pytest    # run before sending a change; bridge tests need Node.js
```

The design, the module boundaries and how to run the tool by hand are described in [How harvesting-dune works](HOW-IT-WORKS.md).

## Building the installer

- **Locally:** install [Inno Setup 6](https://jrsoftware.org/isinfo.php), then run `iscc /DAppVersion=1.2.3 installer\harvesting-dune.iss`. The output goes to `dist\`.
- **ZIP:** run `powershell -ExecutionPolicy Bypass -File installer\build-zip.ps1 -Version 1.2.3`. The output goes to `dist\`. `installer\test-zip.ps1` installs it, installs it a second time and then uninstalls it.
- **GitHub:** set `version` in `pyproject.toml` to the release number and run `uv lock`, then publish a release with that number as its tag. The workflow stops if the tag and the version differ. "Release installer" builds and tests the installer and the ZIP and attaches them to the release. You can also run it by hand from the Actions tab to get a test build without a release.
- Every push runs "CI", which covers the tests, lint and a PowerShell script check.
