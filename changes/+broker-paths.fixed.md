The transport no longer defaults to environment-specific paths for the
credential broker and the token-bucket pacer. Both locations are now
opt-in through TAKEN_CREDENTIAL_BROKER_DIR and TAKEN_THROTTLE_BIN_DIR;
when unset, auth falls back to GH_TOKEN/GITHUB_TOKEN and pacing relies
on header-aware backoff alone.
