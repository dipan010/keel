-- 0004 name uniqueness applies to live deployments only
--
-- Delete is a SOFT delete: the row and its events survive, because "why did
-- this endpoint disappear" has to stay answerable afterwards.
--
-- But uniq_name and the route_name unique were unconditional, while the
-- application's name_taken() check filtered on status <> 'deleted'. So the two
-- disagreed about what "taken" means: the API would report a name as free,
-- then the insert would fail with a constraint violation and a 500. A name
-- could never be reused after a teardown.
--
-- Partial unique indexes make the schema agree with the code. Postgres cannot
-- make a UNIQUE *constraint* partial, so these become indexes.
--
-- uniq_idem is deliberately NOT made partial: an idempotency key that has been
-- used stays used, so a client replaying a key after a teardown gets the
-- original outcome rather than silently creating a second deployment.

begin;

alter table deployments drop constraint uniq_name;
alter table deployments drop constraint deployments_route_name_key;

create unique index deployments_name_live
    on deployments (team_id, name)
    where status <> 'deleted';

create unique index deployments_route_live
    on deployments (route_name)
    where status <> 'deleted';

commit;
