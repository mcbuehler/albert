-- Split customers.name into first, middle (optional) and last name.
alter table customers
    add column first_name  text,
    add column middle_name text,
    add column last_name   text;

-- Backfill existing (test) rows: first word -> first_name, rest -> last_name.
-- Single-word names get the same word in both; fix those in the table editor if needed.
update customers set
    first_name = split_part(trim(name), ' ', 1),
    last_name  = coalesce(nullif(trim(substr(trim(name), length(split_part(trim(name), ' ', 1)) + 1)), ''),
                          split_part(trim(name), ' ', 1));

alter table customers
    alter column first_name set not null,
    alter column last_name  set not null,
    add constraint customers_first_name_check  check (length(trim(first_name)) > 0),
    add constraint customers_middle_name_check check (middle_name is null or length(trim(middle_name)) > 0),
    add constraint customers_last_name_check   check (length(trim(last_name)) > 0),
    drop column name;
