-- The dispatcher inserts the attempt before dialing (so a second dispatcher run sees the line as busy)
-- and fills in the Vapi call id once Vapi accepted the call.
alter table call_attempts alter column vapi_call_id drop not null;
