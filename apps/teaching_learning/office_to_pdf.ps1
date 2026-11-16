param(
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = "Stop"
$word = $null
$document = $null

try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $word.Options.UpdateLinksAtOpen = $false
    $document = $word.Documents.Open($SourcePath, $false, $true, $false)
    $document.ExportAsFixedFormat($OutputPath, 17)
}
finally {
    if ($document -ne $null) {
        try {
            $document.Close(0)
        }
        finally {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
        }
    }
    if ($word -ne $null) {
        try {
            $word.Quit()
        }
        finally {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word)
        }
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
