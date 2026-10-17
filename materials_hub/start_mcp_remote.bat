@echo off
setlocal EnableDelayedExpansion
rem ==========================================================================
rem  Materials Hub external MCP gateway launcher (Streamable HTTP)
rem  Reuses the mcp SDK from subtitle_pipeline venv. Open mode by default.
rem  Override via environment variables:  MCP_HOST  MCP_PORT  MCP_TOKEN
rem    set MCP_TOKEN=xxx && start_mcp_remote.bat    (enables Bearer auth)
rem  NOTE: keep this file pure ASCII (cmd.exe reads .bat as GBK on zh-CN).
rem ==========================================================================
cd /d "%~dp0"

set "PY=%~dp0..\subtitle_pipeline\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [mcp-remote] venv python not found, falling back to python on PATH
  set "PY=python"
)

if "%MCP_HOST%"=="" set "MCP_HOST=0.0.0.0"
if "%MCP_PORT%"=="" set "MCP_PORT=8002"

set "TOKENARG="
if not "%MCP_TOKEN%"=="" set "TOKENARG=--token %MCP_TOKEN%"

echo [mcp-remote] serving http://%MCP_HOST%:%MCP_PORT%/mcp  token_required=%MCP_TOKEN%
"%PY%" "%~dp0mcp_remote.py" --transport streamable-http --host %MCP_HOST% --port %MCP_PORT% %TOKENARG%

rem  Client examples:
rem    Claude / Cursor / Codex (streamable-http):  url = http://HOST:%MCP_PORT%/mcp
rem      header: Authorization: Bearer TOKEN      (omit TOKEN in open mode)
rem    Local stdio:  "%PY%" "%~dp0mcp_remote.py" --transport stdio %TOKENARG%
rem    Health check: curl http://127.0.0.1:%MCP_PORT%/healthz
