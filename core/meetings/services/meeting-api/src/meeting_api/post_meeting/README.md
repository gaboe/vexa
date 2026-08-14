# Post-meeting job domain

`jobs.py` defines the transport-free post-meeting job identity, lease state, and in-memory semantic test double. Durable storage and worker-facing lease endpoints are owned by `admin-api`; meeting-api must use that boundary rather than writing the shared job table directly.
