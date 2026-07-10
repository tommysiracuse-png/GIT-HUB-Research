$ErrorActionPreference = "Continue"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $ProjectRoot "runs"
$PidPath = Join-Path $RunsDir "radar_forever.pid"
$HeartbeatPath = Join-Path $RunsDir "radar_heartbeat.json"
$StatePath = Join-Path $RunsDir "radar_state_latest.json"
$BacklogPath = Join-Path $RunsDir "improvement_backlog.md"
$GrowthPath = Join-Path $RunsDir "growth_plan.md"
$HunterPath = Join-Path $RunsDir "market_hunter_plan.md"
$LlmStatePath = Join-Path $RunsDir "llm_state_packet.md"
$LlmInboxPath = Join-Path $RunsDir "llm_recommendations_inbox.jsonl"
$LlmSwarmPath = Join-Path $RunsDir "llm_swarm_latest.json"
$MemoryPath = Join-Path $RunsDir "memory_facts_latest.md"
$VenueHealthPath = Join-Path $RunsDir "crypto_venue_health.json"
$PredictionPath = Join-Path $RunsDir "prediction_markets_latest.json"
$SelfImprovementReportPath = Join-Path $RunsDir "self_improvement_report.md"
$SelfImprovementReportJsonPath = Join-Path $RunsDir "self_improvement_report.json"
$SelfImprovementTimelinePath = Join-Path $RunsDir "self_improvement_timeline.jsonl"
$LogPath = Join-Path $RunsDir "radar_forever.log"

if (Test-Path $PidPath) {
    $pidValue = Get-Content $PidPath | Select-Object -First 1
    $process = if ($pidValue) { Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue } else { $null }
    if ($process) {
        Write-Output "Radar supervisor: RUNNING pid=$pidValue"
    }
    else {
        Write-Output "Radar supervisor: STALE PID pid=$pidValue"
    }
}
else {
    Write-Output "Radar supervisor: NOT RUNNING"
}

Write-Output "Project root: $ProjectRoot"
Write-Output "Learning DB:   $(Join-Path $RunsDir 'radar.sqlite')"
Write-Output "Latest state:  $StatePath"
Write-Output "Backlog:       $BacklogPath"
Write-Output "Growth plan:   $GrowthPath"
Write-Output "Hunter plan:   $HunterPath"
Write-Output "LLM state:     $LlmStatePath"
Write-Output "LLM inbox:     $LlmInboxPath"
Write-Output "LLM swarm:     $LlmSwarmPath"
Write-Output "Memory:        $MemoryPath"
Write-Output "Venue health:  $VenueHealthPath"
Write-Output "Pred markets:  $PredictionPath"
Write-Output "Log:           $LogPath"

if (Test-Path $HeartbeatPath) {
    Write-Output ""
    Write-Output "Heartbeat:"
    Get-Content $HeartbeatPath
}

if (Test-Path $StatePath) {
    Write-Output ""
    Write-Output "Latest summary:"
    $state = Get-Content $StatePath -Raw | ConvertFrom-Json
    $state.summary | ConvertTo-Json -Depth 4
    if ($state.execution_summary) {
        Write-Output ""
        Write-Output "Execution:"
        $state.execution_summary | ConvertTo-Json -Depth 5
    }
    if ($state.llm_inbox) {
        Write-Output ""
        Write-Output "LLM inbox:"
        $state.llm_inbox | ConvertTo-Json -Depth 5
    }
    if ($state.llm_cost_summary) {
        Write-Output ""
        Write-Output "LLM cost:"
        $state.llm_cost_summary | ConvertTo-Json -Depth 5
    }
    if ($state.self_improvement) {
        Write-Output ""
        Write-Output "Self-improvement:"
        $progress = $null
        if (Test-Path $SelfImprovementReportJsonPath) {
            try {
                $progress = (Get-Content $SelfImprovementReportJsonPath -Raw | ConvertFrom-Json).progress_summary
            }
            catch {
                $progress = $null
            }
        }
        $summary = [ordered]@{
            consumed = @($state.self_improvement.consumed).Count
            evaluated = @($state.self_improvement.evaluated).Count
            active_policies = @($state.self_improvement.active_policies).Count
            experiments = @($state.self_improvement.experiments).Count
            route_probe_tasks = @($state.self_improvement.route_probe_tasks).Count
            adapter_specs = @($state.self_improvement.adapter_specs).Count
            report = $SelfImprovementReportPath
            timeline = $SelfImprovementTimelinePath
        }
        if ($progress) {
            $summary["policy_status_counts"] = $progress.policy_status_counts
            $summary["experiment_status_counts"] = $progress.experiment_status_counts
            $summary["paper_entries_filtered"] = $progress.policy_impact.paper_entries_filtered
            $summary["paper_entries_opened_under_policy"] = $progress.policy_impact.paper_entries_opened_under_policy
            $summary["estimated_paper_notional_blocked_or_reduced_usd"] = $progress.policy_impact.estimated_total_risk_reduction_usd
            $summary["all_time_avg_pnl_bps"] = $progress.all_time.avg_pnl_bps
            $summary["since_first_auto_policy_avg_pnl_bps"] = $progress.since_first_activation.avg_pnl_bps
            $summary["since_vs_all_time_avg_pnl_delta_bps"] = $progress.since_vs_all_time_avg_pnl_delta_bps
        }
        $summary | ConvertTo-Json -Depth 7
    }
    if ($null -ne $state.memory_facts_added) {
        Write-Output ""
        Write-Output "Memory facts added this loop: $($state.memory_facts_added)"
    }
    if ($state.horizon_outcomes) {
        Write-Output ""
        Write-Output "Horizon outcomes:"
        $state.horizon_outcomes | ConvertTo-Json -Depth 5
    }
    if ($state.llm_swarm_generated) {
        Write-Output ""
        Write-Output "LLM swarm generated:"
        $state.llm_swarm_generated | Select-Object -First 5 | ConvertTo-Json -Depth 6
    }
}

if (Test-Path $BacklogPath) {
    Write-Output ""
    Write-Output "Open backlog:"
    Get-Content $BacklogPath | Select-Object -First 12
}

if (Test-Path $GrowthPath) {
    Write-Output ""
    Write-Output "Growth plan:"
    Get-Content $GrowthPath | Select-Object -First 18
}

if (Test-Path $HunterPath) {
    Write-Output ""
    Write-Output "Hunter plan:"
    Get-Content $HunterPath | Select-Object -First 18
}

if (Test-Path $LlmStatePath) {
    Write-Output ""
    Write-Output "LLM state packet:"
    Get-Content $LlmStatePath | Select-Object -First 18
}
