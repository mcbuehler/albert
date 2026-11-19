# Deployment

Import the repository as a Vercel project and set the [environment variables](configuration.md) there. The supplied [`vercel.json`](../vercel.json) runs the dispatcher and call archive every minute, which requires a plan that supports minute-level cron jobs. Vercel sends `CRON_SECRET` in the bearer header. On another host, schedule authenticated GET requests to `/api/cron/dispatch` and `/api/cron/archive`. Staff can run either job from the **Debug** tab. [Vercel cron limits](https://vercel.com/docs/schedules/cron-jobs/usage-and-pricing)

Add the deployment URL to Supabase Auth redirects and configure Vapi to use that URL. Run the [smoke test](vapi-setup.md#smoke-test) after deployment.
