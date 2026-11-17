param(
    [uri]$Server = 'http://127.0.0.1:8787',
    [ValidateRange(1, 600)][int]$TimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
if (!$Server.IsAbsoluteUri -or !$Server.Host -or $Server.UserInfo -or $Server.Query -or $Server.Fragment -or
    ($Server.Scheme -ne 'https' -and !($Server.IsLoopback -and $Server.Scheme -eq 'http'))) {
    throw 'Use an HTTPS server URL or a local HTTP loopback address.'
}
if ([string]::IsNullOrWhiteSpace($env:OPEN_NPU_API_KEY)) {
    throw 'Set OPEN_NPU_API_KEY to the key for an approved project.'
}
$base = $Server.AbsoluteUri.TrimEnd('/')
$headers = @{ Authorization = "Bearer $env:OPEN_NPU_API_KEY" }
$submissionHeaders = $headers + @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$body = @{ capsule = 'square-fp16-v1'; values = @(2, -3, 4) } | ConvertTo-Json -Compress
$job = Invoke-RestMethod "$base/v1/jobs" -Method Post -Headers $submissionHeaders -ContentType 'application/json' -Body $body -TimeoutSec 10 -MaximumRedirection 0
if ($job.id -notmatch '^[0-9a-f]{32}$') {
    throw 'The server did not return a valid job identifier.'
}
$watch = [System.Diagnostics.Stopwatch]::StartNew()
do {
    $result = Invoke-RestMethod "$base/v1/jobs/$($job.id)" -Headers $headers -TimeoutSec 10 -MaximumRedirection 0
    if ($result.status -eq 'done') {
        if ($result.values.Count -ne 3 -or $result.values[0] -ne 4 -or $result.values[1] -ne 9 -or $result.values[2] -ne 16) {
            throw "Job $($job.id) did not return the expected values [4, 9, 16]."
        }
        $result | ConvertTo-Json -Depth 4
        return
    }
    if ($result.status -notin @('queued', 'running')) {
        throw "Job $($job.id) ended with status '$($result.status)'."
    }
    Start-Sleep -Seconds 2
} while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds)
throw "Job $($job.id) is still $($result.status). Check that a compatible node is available for the project."
