@{
    # Repo-local PSScriptAnalyzer settings.
    # Excluding PSAvoidAssignmentToAutomaticVariable because it is producing false-positives
    # in this workspace (reporting $Host/$args assignment where none exists).
    ExcludeRules = @(
        'PSAvoidAssignmentToAutomaticVariable',
        # Many operational scripts intentionally use Write-Host for operator-facing,
        # colorized output (and are not intended to be pipeline cmdlets).
        'PSAvoidUsingWriteHost',

        # Many scripts use best-effort cleanup where failure should be ignored.
        # Empty catch blocks are intentional in these specific cases.
        'PSAvoidUsingEmptyCatchBlock',
        # This repo uses several internal helper cmdlets in scripts; enforceability is low
        # and VS Code diagnostics can get stuck on renamed functions.
        'PSUseApprovedVerbs'
    )
}
