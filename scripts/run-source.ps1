$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw 'Checkout venv missing. Create .venv with Python 3.12+ and install this checkout with pip install -e .'
}
& $taskPython -m acp_gateway @args
exit $LASTEXITCODE
