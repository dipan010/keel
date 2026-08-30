-- 0002 idempotency fingerprint
--
-- uniq_idem already stops a replayed Idempotency-Key from creating a second
-- deployment. It does not stop a client from reusing a key with a DIFFERENT
-- body -- which would silently return a deployment that is not the one they
-- just asked for. Storing a fingerprint of the request lets us tell the two
-- apart: same key + same body is a replay, same key + different body is a
-- client bug and gets a 409.

begin;

alter table deployments add column request_fingerprint text;

commit;
