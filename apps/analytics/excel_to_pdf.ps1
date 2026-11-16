param(
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = "Stop"
$excel = $null
$workbook = $null

try {
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    $excel.AutomationSecurity = 3
    $workbook = $excel.Workbooks.Open($SourcePath, 0, $true)
    $workbook.ExportAsFixedFormat(0, $OutputPath)
}
finally {
    if ($workbook -ne $null) {
        try { $workbook.Close($false) }
        finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($workbook) }
    }
    if ($excel -ne $null) {
        try { $excel.Quit() }
        finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($excel) }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
