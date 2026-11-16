# iis_setup.ps1
# This script configures IIS to point to the stock-dashboard directory and enables ARR Proxy.
# MUST BE RUN AS ADMINISTRATOR!

Import-Module WebAdministration

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "1. Checking IIS Sites and Paths..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$sites = Get-ChildItem IIS:\Sites
foreach ($site in $sites) {
    Write-Host "Site Name: $($site.Name)" -ForegroundColor Yellow
    Write-Host "  State: $($site.State)"
    Write-Host "  Physical Path: $($site.physicalPath)"
    Write-Host "  Bindings: $($site.Bindings.Collection)"
}

Write-Host "`n==========================================" -ForegroundColor Cyan
Write-Host "2. Updating 'Default Web Site' Physical Path..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

$targetPath = "C:\Users\cyutim\claude code\stock-dashboard"

if (Test-Path $targetPath) {
    try {
        Set-ItemProperty "IIS:\Sites\Default Web Site" -Name physicalPath -Value $targetPath
        Write-Host "[SUCCESS] Set 'Default Web Site' physical path to: $targetPath" -ForegroundColor Green
    } catch {
        Write-Error "Failed to update IIS site path. Make sure this script is run as Administrator!"
        exit 1
    }
} else {
    Write-Error "Target path $targetPath does not exist!"
    exit 1
}

Write-Host "`n==========================================" -ForegroundColor Cyan
Write-Host "3. Enabling ARR Proxy (for Reverse Proxy rewriting)..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

try {
    # Check if system.webServer/proxy configuration section exists (requires ARR)
    $proxySection = Get-WebConfiguration -Filter "system.webServer/proxy" -PSPath "MACHINE/WEBROOT/APPHOST"
    if ($proxySection) {
        Set-WebConfigurationProperty -pspath 'MACHINE/WEBROOT/APPHOST' -filter "system.webServer/proxy" -name "enabled" -value $true
        Write-Host "[SUCCESS] Application Request Routing (ARR) Proxy enabled successfully!" -ForegroundColor Green
    } else {
        Write-Host "[WARNING] system.webServer/proxy section not found. Please ensure IIS Application Request Routing (ARR) is installed!" -ForegroundColor Red
    }
} catch {
    Write-Host "[WARNING] Could not configure ARR Proxy: $_" -ForegroundColor Yellow
}

Write-Host "`n==========================================" -ForegroundColor Cyan
Write-Host "4. Restarting IIS Service (w3svc)..." -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

try {
    Restart-Service w3svc -Force
    Write-Host "[SUCCESS] IIS (w3svc) service restarted successfully!" -ForegroundColor Green
} catch {
    Write-Host "[WARNING] Failed to restart w3svc service: $_" -ForegroundColor Yellow
}

Write-Host "`n==========================================" -ForegroundColor Cyan
Write-Host "Configuration completed. Please test accessing http://localhost in your browser!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
