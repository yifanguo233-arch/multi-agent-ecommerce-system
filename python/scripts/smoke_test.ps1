param(
    [string]$ApiHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$UserId = "u001",
    [int]$NumItems = 5,
    [string]$ExpectABEnabled = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Python venv not found: $python"
}

$outLog = Join-Path $root "tmp_uvicorn_out.log"
$errLog = Join-Path $root "tmp_uvicorn_err.log"
if (Test-Path $outLog) { Remove-Item $outLog -Force }
if (Test-Path $errLog) { Remove-Item $errLog -Force }

$base = "http://$ApiHost`:$Port"

Write-Host "Starting API server at $base ..."
$proc = Start-Process -FilePath $python `
    -ArgumentList "-m", "uvicorn", "main:app", "--host", $ApiHost, "--port", "$Port" `
    -WorkingDirectory $root `
    -PassThru `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden

Start-Sleep -Seconds 4

try {
    $checks = @()

    Write-Host "`n[1/5] GET /health"
    $health = Invoke-RestMethod -Uri "$base/health" -Method Get
    $health | ConvertTo-Json -Depth 6
    $checks += [pscustomobject]@{
        name = "health_status"
        pass = ($health.status -eq "healthy")
        detail = "status=$($health.status)"
    }
    $checks += [pscustomobject]@{
        name = "health_model_present"
        pass = (-not [string]::IsNullOrWhiteSpace([string]$health.model))
        detail = "model=$($health.model)"
    }

    $bodyObj = @{
        user_id = $UserId
        scene = "homepage"
        num_items = $NumItems
        context = @{
            recent_views = @("手机", "耳机")
            purchase_count_30d = 2
            avg_order_amount = 699.0
        }
    }
    $body = $bodyObj | ConvertTo-Json -Depth 8

    Write-Host "`n[2/5] POST /api/v1/recommend"
    $rec = Invoke-RestMethod -Uri "$base/api/v1/recommend" -Method Post -ContentType "application/json" -Body $body
    @{
        request_id = $rec.request_id
        experiment_group = $rec.experiment_group
        product_count = @($rec.products).Count
        copy_count = @($rec.marketing_copies).Count
        total_latency_ms = $rec.total_latency_ms
    } | ConvertTo-Json -Depth 6
    $checks += [pscustomobject]@{
        name = "recommend_has_products"
        pass = (@($rec.products).Count -gt 0)
        detail = "product_count=$(@($rec.products).Count)"
    }
    $checks += [pscustomobject]@{
        name = "recommend_has_copies"
        pass = (@($rec.marketing_copies).Count -gt 0)
        detail = "copy_count=$(@($rec.marketing_copies).Count)"
    }
    $checks += [pscustomobject]@{
        name = "recommend_group_present"
        pass = (-not [string]::IsNullOrWhiteSpace([string]$rec.experiment_group))
        detail = "group=$($rec.experiment_group)"
    }

    Write-Host "`n[3/5] POST /api/v1/recommend/graph"
    $graph = Invoke-RestMethod -Uri "$base/api/v1/recommend/graph" -Method Post -ContentType "application/json" -Body $body
    @{
        request_id = $graph.request_id
        experiment_group = $graph.experiment_group
        product_count = @($graph.products).Count
        copy_count = @($graph.marketing_copies).Count
        total_latency_ms = $graph.total_latency_ms
    } | ConvertTo-Json -Depth 6
    $checks += [pscustomobject]@{
        name = "graph_has_products"
        pass = (@($graph.products).Count -gt 0)
        detail = "product_count=$(@($graph.products).Count)"
    }
    $checks += [pscustomobject]@{
        name = "graph_has_copies"
        pass = (@($graph.marketing_copies).Count -gt 0)
        detail = "copy_count=$(@($graph.marketing_copies).Count)"
    }

    Write-Host "`n[4/5] GET /api/v1/experiments"
    $experiments = Invoke-RestMethod -Uri "$base/api/v1/experiments" -Method Get
    $experimentKeys = @($experiments.PSObject.Properties.Name | Where-Object { $null -ne $_ -and $_ -ne "" })
    $experimentKeys
    $checks += [pscustomobject]@{
        name = "experiments_present"
        pass = ($experimentKeys.Count -gt 0)
        detail = "count=$($experimentKeys.Count)"
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectABEnabled)) {
        $normalized = $ExpectABEnabled.Trim().ToLower()
        if ($normalized -notin @("true", "false")) {
            throw "ExpectABEnabled must be 'true' or 'false', got '$ExpectABEnabled'"
        }
        $expected = ($normalized -eq "true")
        $mismatches = @()
        foreach ($k in $experimentKeys) {
            $exp = $experiments.$k
            if ($null -eq $exp -or $exp.enabled -ne $expected) {
                $actual = if ($null -eq $exp) { "null" } else { [string]$exp.enabled }
                $mismatches += "$k=$actual"
            }
        }
        $checks += [pscustomobject]@{
            name = "experiments_enabled_matches_expected"
            pass = ($mismatches.Count -eq 0)
            detail = "expected=$expected mismatches=$($mismatches -join ',')"
        }
    }

    Write-Host "`n[5/5] GET /api/v1/metrics"
    $metrics = Invoke-RestMethod -Uri "$base/api/v1/metrics" -Method Get
    $businessKeys = @()
    if ($null -ne $metrics.business -and $null -ne $metrics.business.PSObject) {
        $businessKeys = @($metrics.business.PSObject.Properties.Name | Where-Object { $null -ne $_ -and $_ -ne "" })
    }
    @{
        agent_keys = @($metrics.agents.PSObject.Properties.Name)
        business_keys = $businessKeys
    } | ConvertTo-Json -Depth 6
    $agentKeys = @($metrics.agents.PSObject.Properties.Name | Where-Object { $null -ne $_ -and $_ -ne "" })
    $checks += [pscustomobject]@{
        name = "metrics_agent_keys"
        pass = ($agentKeys.Count -ge 4)
        detail = "count=$($agentKeys.Count)"
    }

    Write-Host "`n[Summary] Assertions"
    foreach ($c in $checks) {
        $flag = if ($c.pass) { "PASS" } else { "FAIL" }
        Write-Host ("{0} - {1} ({2})" -f $flag, $c.name, $c.detail)
    }

    $failed = @($checks | Where-Object { -not $_.pass })
    if ($failed.Count -gt 0) {
        Write-Host "`nSmoke test verdict: FAIL"
        exit 1
    }
    Write-Host "`nSmoke test verdict: PASS"

    Write-Host "`nSmoke test completed."
}
finally {
    if ($proc -and (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $proc.Id -Force
    }
    Write-Host "Server stopped."
}
