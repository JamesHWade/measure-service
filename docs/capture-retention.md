# Immutable capture retention

Batch snapshot exports and chromatogram evidence reads use separate
process-local caches. A capture holds one immutable snapshot so later pages do
not reread or rebuild the source document. Captures are bound to the database
file identity and authenticated owner. A replaced database file has a new
identity even when its path is unchanged.

Each cache retains at most four captures for one owner and at most 64 captures
across all owners. The batch export cache accounts for at most 128 MiB of
serialized snapshots; the evidence cache accounts for at most 64 MiB. The
existing batch export page limit remains 48,000 bytes. These limits apply
independently to the two caches.

An entry expires after five minutes without a read. Reading a page refreshes
that entry's idle time and least-recently-used position. Expired entries may
be removed for any owner. To make room for a new capture, the service may
evict the least-recently-used capture belonging to the requesting owner. It
does not evict another owner's unexpired capture. If the global count or byte
limit cannot be met using the requesting owner's captures, the new capture is
rejected and its existing captures remain available.

Captures are held only in the current service process. A restart clears them.
An unknown, evicted, expired, or prior-store capture returns the same not-found
response and asks the caller to capture a new snapshot or manifest. The
response does not disclose whether another owner has a capture with the same
digest.
