$ErrorActionPreference = 'Stop'
$taskUv = (Get-Command uv -ErrorAction Stop).Source
$taskHelper = Join-Path $PSScriptRoot 'checkout.py'
& $taskUv run --no-project --python 3.12 --managed-python $taskHelper update @args
exit $LASTEXITCODE
