-- LMS Portfolio: store Product/Mode with each holding lot.
-- Recommended once on the new Supabase project.
alter table public.holdings
add column if not exists product text default 'NORMAL';

update public.holdings
set product = 'NORMAL'
where product is null or trim(product) = '';

create index if not exists idx_holdings_product
on public.holdings (product);
