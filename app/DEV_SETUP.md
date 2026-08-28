# Developer setup (Windows)

This app is built with [Tauri](https://tauri.app) (Rust backend + React/TS frontend). To build it on
Windows **without requiring Administrator/UAC elevation** (e.g. in a locked-down or shared machine),
use the GNU Rust toolchain backed by MSYS2's `mingw-w64` GCC + `lld`, instead of the default MSVC
toolchain (which requires the Visual Studio "Desktop development with C++" workload, normally an
elevated install).

## One-time toolchain setup (no admin required)

```powershell
# 1. Install Rust (rustup) - user-scoped, no admin needed
winget install --id Rustlang.Rustup

# 2. Install MSYS2 (user-scoped, no admin needed)
winget install --id MSYS2.MSYS2 --scope user

# 3. Install mingw-w64 GCC + lld via MSYS2's pacman
C:\msys64\usr\bin\bash.exe -lc "pacman -Sy --noconfirm"
C:\msys64\usr\bin\bash.exe -lc "pacman -S --noconfirm mingw-w64-x86_64-gcc mingw-w64-x86_64-lld"

# 4. Point Rust at the GNU toolchain (avoids needing MSVC Build Tools)
rustup toolchain install stable-x86_64-pc-windows-gnu
rustup default stable-x86_64-pc-windows-gnu

# 5. Add cargo + mingw64 to your PATH (persist for future shells)
[Environment]::SetEnvironmentVariable("Path", "C:\msys64\mingw64\bin;$([Environment]::GetEnvironmentVariable('Path','User'))", "User")
```

Restart your terminal after step 5 so the PATH change takes effect.

## Why not the default MSVC toolchain?

Rust's default Windows target (`x86_64-pc-windows-msvc`) requires the MSVC linker, which comes from
the Visual Studio "Desktop development with C++" workload. Installing that workload (or modifying an
existing Visual Studio install to add it) requires Administrator/UAC elevation. The GNU toolchain
(`x86_64-pc-windows-gnu`) uses MinGW's GCC/`ld` instead, which MSYS2 installs entirely into the user's
profile — no elevation required. If you already have the MSVC Build Tools installed, you can skip all
of this and just use the default `x86_64-pc-windows-msvc` toolchain instead.

## Known linker quirk with mingw + Tauri (`--exclude-all-symbols`)

By default, MinGW's `ld` tries to export every public Rust symbol from the app's `.exe`/`.dll`, which
overflows the 16-bit Windows export-ordinal table once Tauri's dependency tree is linked in
(`export ordinal too large` / `too many exported symbols` errors). This repo's
`src-tauri/.cargo/config.toml` works around it by switching the linker to LLVM's `lld` (also installed
via MSYS2 above) and passing `-Wl,--exclude-all-symbols` so only symbols Tauri explicitly marks for
export are emitted:

```toml
[target.x86_64-pc-windows-gnu]
rustflags = [
    "-C", "link-arg=-fuse-ld=lld",
    "-C", "link-arg=-Wl,--exclude-all-symbols",
]
```

## Known path-with-spaces quirk

If your repo checkout path contains spaces (e.g. `...\SRE Starter Demo\...`), the mingw/lld linker
invocation can mis-tokenize the path in some setups. If you hit `ld.lld: error: could not open '...'`
errors referencing a truncated path, set `CARGO_TARGET_DIR` to a path without spaces before building:

```powershell
$env:CARGO_TARGET_DIR = "D:\sre-agent-target"
npm run tauri build
```

## Building

```powershell
cd app
npm install
npm run tauri build        # release build -> installers in src-tauri/target/release/bundle/ (or $CARGO_TARGET_DIR)
npm run tauri dev           # dev mode with hot reload
```

Release builds produce both an NSIS `.exe` installer and a WiX `.msi`, targeting Windows 11's built-in
WebView2 runtime (no bundled Chromium/Node/Python).
