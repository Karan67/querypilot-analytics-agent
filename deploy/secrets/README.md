# Mount point for file-based secrets (Iteration 12 T8).
#
# Anything in this directory is mounted read-only at /run/secrets in the API
# container. Point GROQ_API_KEY_FILE or QUERYPILOT_USERS_FILE at a path under
# /run/secrets to use the file form instead of an environment variable.
#
# The directory is tracked so the bind mount always has something to mount;
# its contents are not.
