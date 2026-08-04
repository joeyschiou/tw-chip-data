while ($true) {
    Add-Content -Path "$PSScriptRoot\rc_log.txt" -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') resuming session"
    claude --continue
    Add-Content -Path "$PSScriptRoot\rc_log.txt" -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') exited, restart in 5s"
    Start-Sleep 5
}
