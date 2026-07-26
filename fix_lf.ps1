$files = @("backend/entrypoint.sh", "start.sh", "stop.sh")
foreach ($f in $files) {
    if (Test-Path $f) {
        $content = [System.IO.File]::ReadAllText((Resolve-Path $f))
        $content = $content.Replace("`r`n", "`n")
        [System.IO.File]::WriteAllBytes((Resolve-Path $f), [System.Text.Encoding]::UTF8.GetBytes($content))
        Write-Host "Fixed LF for $f"
    }
}
