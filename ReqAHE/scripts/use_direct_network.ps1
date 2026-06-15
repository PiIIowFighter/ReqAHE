# Force ReqAHE LLM calls to use local direct network instead of VPN/system proxy ports.
$env:OPENAI_TRUST_ENV = "false"
$env:OPENAI_NO_PROXY = "open.bigmodel.cn,localhost,127.0.0.1,<local>"

foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")) {
    if (Test-Path "Env:$name") {
        Remove-Item "Env:$name"
    }
}

$extraNoProxy = "open.bigmodel.cn,localhost,127.0.0.1,<local>"
if ($env:NO_PROXY) {
    $env:NO_PROXY = "$($env:NO_PROXY),$extraNoProxy"
} else {
    $env:NO_PROXY = $extraNoProxy
}
$env:no_proxy = $env:NO_PROXY

Write-Host "[network] direct mode enabled: trust_env=false, proxy env cleared, NO_PROXY=$($env:NO_PROXY)"
