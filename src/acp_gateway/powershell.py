"""Compact fixed PowerShell bootstrap; dynamic inputs remain on JSON stdin."""

import base64
import gzip


def encoded_script(script):
    compressed = base64.b64encode(gzip.compress(script.encode("utf-8"), mtime=0)).decode("ascii")
    bootstrap = (
        "$b=[Convert]::FromBase64String('" + compressed + "');"
        "$m=New-Object IO.MemoryStream(,$b);"
        "$g=New-Object IO.Compression.GZipStream($m,[IO.Compression.CompressionMode]::Decompress);"
        "$r=New-Object IO.StreamReader($g,[Text.Encoding]::UTF8);"
        "$s=$r.ReadToEnd();$r.Dispose();& ([ScriptBlock]::Create($s))"
    )
    return base64.b64encode(bootstrap.encode("utf-16-le")).decode("ascii")
