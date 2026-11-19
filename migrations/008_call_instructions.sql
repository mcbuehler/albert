-- Optional staff instructions for a scheduled callback, passed to the assistant as {{call_instructions}}.
-- Safe to re-run.
alter table scheduled_calls add column if not exists instructions text;

alter table scheduled_calls drop constraint if exists scheduled_calls_instructions_check;
alter table scheduled_calls add constraint scheduled_calls_instructions_check
    check (instructions is null or (length(instructions) between 1 and 1000));
