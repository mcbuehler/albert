# Set up Albert in Vapi

Start with the [backend quickstart](../README.md#quickstart) and a Vapi organization for this deployment.
Vapi needs to reach the Flask backend at a public HTTPS URL. Use a phone connection that supports
GPT-Live, such as native Twilio or Vapi SIP. [GPT-Live compatibility](https://docs.vapi.ai/gpt-live/configuration)

## Connect Vapi to the backend

1. Set `VAPI_API_KEY` on the backend to a private API key from your Vapi organization. Albert uses it
   to list resources, place outbound calls, and retrieve call artifacts.
2. Generate a separate value for `REL8_API_AUTH_TOKEN` and set it on the backend. In Vapi, create a
   custom **Bearer Token** credential with that value. Use the credential for the phone number's
   server URL, each assistant's server URL, and the API Request tools. For API configuration, set
   `server.credentialId` on the phone number and assistants, and `credentialId` on API Request tools.
   [Vapi server authentication](https://docs.vapi.ai/server-url/server-authentication)

## Create the tools

In Vapi, create the API Request tools in the table. Prefix each request path with your backend URL.
The GET tools use the named model inputs in the URL. Define those inputs in the tool's request schema.
For each POST tool, use a JSON object for the model inputs and add `customer_number` as a **Static Body
Field** with the value `{{customer.number}}`. This lets Vapi supply the actual caller number. In the
API, the static field is `"parameters": [{"key": "customer_number", "value": "{{customer.number}}"}]`.
[Vapi request configuration](https://docs.vapi.ai/tools/api-request/configuration)

| Template placeholder | Vapi tool name | Request | Model inputs |
| --- | --- | --- | --- |
| `GET_WEATHER_TOOL_ID` | `get_weather` | `GET /api/tools/weather/{{city}}/{{language}}` | `city` and `language` strings. Use `en` or `de` for `language`. |
| `SEARCH_WEB_TOOL_ID` | `search_web` | `GET /api/tools/search/{{query}}` | `query` string |
| `SCHEDULE_CALLBACK_TOOL_ID` | `schedule_callback` | `POST /api/tools/schedule-callback` | `local_time` string and optional `instructions` string |
| `SCHEDULE_CALLBACK_DE_TOOL_ID` | `schedule_callback_de` | `POST /api/tools/schedule-callback` | Same inputs as `schedule_callback` |
| `CANCEL_CALLBACK_TOOL_ID` | `cancel_callback` | `POST /api/tools/cancel-callback` | Optional `callback_time` string when only one callback is pending |
| `RESCHEDULE_CALLBACK_TOOL_ID` | `move_callback` | `POST /api/tools/reschedule-callback` | `new_local_time` string and `callback_time` string, which can be omitted when only one callback is pending |
| `ONBOARD_CUSTOMER_TOOL_ID` | `onboard_customer` | `POST /api/tools/onboard-customer` | `first_name` and `last_name` strings, `notes_consent` boolean, and optional `timezone` string such as `Europe/Zurich` |
| `ONBOARD_CUSTOMER_DE_TOOL_ID` | `onboard_customer_de` | `POST /api/tools/onboard-customer` | Same inputs as `onboard_customer` |
| `NOTIFY_TEAM_TOOL_ID` | `notify_team` | `POST /api/tools/notify-team` | `reason` and `summary` strings, and optional `contact_name` and `organization` strings |
| `END_CALL_TOOL_ID` | `end_call` | Vapi `endCall` tool | None |
| `END_CALL_DE_TOOL_ID` | `end_call_de` | Vapi `endCall` tool | None |

Callback times use the customer's local time as `YYYY-MM-DDTHH:MM`, for example `2027-02-15T14:30`.
Use `true` for `notes_consent` after the caller explicitly agrees to memory notes. Set a 30-second
timeout on both onboarding tools so the backend can create the memory bank. For `notify_team`, use
`no_consent`, `institution_lead`, `caregiver_lead`, `request`, or `other` as `reason`. The dispatcher
sends queued notifications when [SMTP is configured](../.env.example).

## Create the assistants and connect a number

1. Copy the [four assistant templates](../vapi/assistants/) to a private working directory. Replace
   the placeholders with the corresponding tool IDs from your Vapi organization in both
   `model.toolIds` and `model.reasoner.skills[].toolIds`. The first list makes tools available for
   every request. Each skill list adds tools when the reasoner loads that skill. Keep the skills and
   both the speaker and reasoner prompts. [Vapi reasoner skills](https://docs.vapi.ai/gpt-live/skills)
2. Replace `support@example.com` with your support address. Set the server URL of each assistant to
   your backend's `/api/webhook`, attach the bearer credential, and include `end-of-call-report` in
   server messages. That report feeds the customer's Hindsight memory.
3. For the outbound `albert-live-en.yml` and `albert-live-de.yml` assistants, replace `{{name}}` in
   the greeting with `{{customer_first_name}}`, which Albert provides. For retry testing, clear the
   sample voicemail message and enable Vapi voicemail detection where supported by your call setup.
   Verify the ended reason on a call that reaches a test mailbox. [Voicemail detection](https://docs.vapi.ai/calls/voicemail-detection)
4. Enter the completed settings in the Vapi dashboard or convert the YAML to JSON and use the Create
   Assistant API. Publish the assistants in your Vapi organization. The templates preserve the source
   voice, transcriber, endpointing, and fallback fields. Use the GPT-Live compatibility reference to
   determine which audio settings apply to this model. [Create Assistant API](https://docs.vapi.ai/api-reference/assistants/create) · [GPT-Live compatibility](https://docs.vapi.ai/gpt-live/configuration)
5. Set the test phone number's server URL to your backend's `/api/webhook` and attach the same
   bearer credential. Leave the number's assistant unassigned so incoming calls send
   `assistant-request` and Albert chooses an assistant. Use an outbound-capable number for callbacks.
   [Vapi server URLs](https://docs.vapi.ai/server-url/setting-server-urls) · [Vapi server events](https://docs.vapi.ai/server-url/events)

Set these backend variables to the IDs Vapi assigned after creating the assistants and numbers.

| Variable | Resource |
| --- | --- |
| `VAPI_ASSISTANT_ID` | Fallback for unknown inbound callers from supported regions. Use the English inbound assistant for an English-only trial. |
| `VAPI_INBOUND_ASSISTANT_ID_US` | English inbound assistant for unknown callers from the US and Canada |
| `VAPI_INBOUND_ASSISTANT_ID_CH` | German inbound assistant for unknown callers from Switzerland |
| `VAPI_ASSISTANT_ID_US`, `VAPI_PHONE_NUMBER_ID_US` | Default assistant and outbound Vapi phone number for new US and Canadian customers who sign up by phone |
| `VAPI_ASSISTANT_ID_CH`, `VAPI_PHONE_NUMBER_ID_CH` | Default assistant and outbound Vapi phone number for new Swiss customers who sign up by phone |

Set only the regional pairs you want to enable for phone sign-up. For a language-matched deployment,
use `albert-live-en.yml` for the US and Canada and `albert-live-de.yml` for Switzerland as the default
customer assistants. Staff can also choose an assistant and number when creating a customer. Albert
selects a recognized caller's saved assistant on later inbound calls. Unknown callers with a visible
US, Canadian, or Swiss number use the regional inbound assistant or fallback.

## Smoke test

1. Sign in to the staff page and verify the customer form lists your Vapi assistants and numbers.
2. Call the test number from a number you control in a supported region with caller ID enabled.
   Check the language and try the weather tool. If phone sign-up is enabled, try consenting to notes
   and verify a new customer appears for staff review. Otherwise create a test customer in the staff
   page to continue.
3. Schedule a callback for the test customer. When it is due, run the dispatcher from the **Debug**
   tab or wait for the scheduled job. Answer the call and share a harmless test detail. Call again
   once Vapi has delivered the end-of-call report and ask Albert to recall it.
4. To test the archive, set `artifactPlan.recordingEnabled` and `artifactPlan.transcriptPlan.enabled`
   to `true` on the test assistants and create the [private recording bucket](../README.md#quickstart).
   Run the archive from **Debug** after the call ends, then open **Calls** and check the transcript and
   playback once the artifacts are available.
   [Vapi call artifacts](https://docs.vapi.ai/assistants/call-recording)

Vapi artifact collection and the Hindsight memory consent step serve different purposes. Albert's
archive scans calls in the Vapi organization, including unknown callers, and stores available
transcripts and recordings when capture is enabled. Review the artifact settings and decide which
calls to record before using a real phone number. [Archive schema](call-archive-schema.md)
