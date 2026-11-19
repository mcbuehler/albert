# Vapi conversation flow and interruption handling

Research captured on 2026-10-25 while investigating the classic Vapi voice pipeline. See the
[Vapi setup guide](vapi-setup.md) for the published Albert Live configurations.

## Problem summary

The classic Vapi assistant stopped speaking when the customer said a word. Its interruption settings treated detected speech as a cue that the customer wanted the floor. Possible inputs included:

- “Uh-huh,” “right,” and “okay” are commonly backchannels.
- “Stop,” “wait,” and sometimes “no” are genuine one-word interruptions.
- Coughs, speakerphone echo, television audio, and nearby voices are false detections.

Vapi's classic default uses `stopSpeakingPlan.numWords: 0`. This selects a VAD-driven interruption path; after approximately `voiceSeconds: 0.2` of voice activity, the assistant can be interrupted. It is fast but intentionally sensitive. Vapi notes that it can respond to filler sounds, throat clearing, and background noise. [`startSpeakingPlan` controls when the assistant begins replying, while `stopSpeakingPlan` controls interruption during playback.](https://docs.vapi.ai/customization/voice-pipeline-configuration)

This distinction matters: better endpointing can keep the assistant from replying during a customer's thinking pause, but it does not by itself prevent “uh-huh” from stopping an answer already in progress.

## Potential solutions, simplest first

| Rank | Approach | Effort | Likely benefit | Main trade-off |
|---:|---|---:|---:|---|
| 1 | Tune Vapi acknowledgement and interruption phrases | Very low | High for recognizable backchannels | Phrase lists are language- and domain-specific |
| 2 | Move from VAD-only to transcription-based interruption | Very low | High | Genuine interruptions take another 200–500 ms |
| 3 | Increase the VAD duration threshold | Very low | Medium | Long one-word backchannels can still interrupt |
| 4 | Enable denoising and check echo paths | Low | High if noise or echo is involved | Cannot distinguish two legitimate speakers semantically |
| 5 | Shorten and restructure assistant responses | Low | Medium–high | Requires prompt and conversation-design changes |
| 6 | Upgrade endpointing/transcription inside Vapi | Low–medium | Improves overall flow; indirect for this symptom | Does not itself classify barge-in intent |
| 7 | Pilot Vapi's native speech-to-speech/full-duplex options | Medium | Potentially very high | Feature and provider constraints |
| 8 | Move to a framework/API with semantic interruption handling | Medium–high | High and controllable | Pipeline migration |
| 9 | Build a two-stage semantic barge-in controller | High | Very high | Significant real-time audio engineering |
| 10 | Native full-duplex/open-model redesign | Very high | Highest theoretical naturalness | Cost, maturity, control, and operational risk |

### 1. Classify acknowledgement and interruption phrases in Vapi

Configure two deliberately small, domain-specific lists:

- `acknowledgementPhrases`: “uh-huh,” “mm-hmm,” “right,” “okay,” “got it,” “makes sense,” plus variants actually found in call recordings.
- `interruptionPhrases`: “stop,” “wait,” “hold on,” “that's wrong,” “no, wait,” and other phrases that should immediately clear playback.

Vapi evaluates explicit interruption phrases, acknowledgement phrases, and then the general threshold. Its Deepgram Flux example combines `numWords: 2` with an acknowledgement list. See the [Vapi pipeline documentation](https://docs.vapi.ai/customization/voice-pipeline-configuration).

Avoid overly broad lists. “Okay” or “no” may be a backchannel in one workflow and a correction in another. Lists must also be localized for every supported language.

### 2. Require two transcribed words before stopping

Test `numWords: 2` instead of the default `0`. This makes the system wait for transcription rather than interrupting solely on voice activity.

Benefits:

- A cough or short sound normally will not stop the agent.
- One-word backchannels will not stop it.
- Acknowledgement phrases can be classified more reliably.

Costs:

- True interruptions are slower.
- A caller saying only “stop” must be covered by `interruptionPhrases`; otherwise it may not meet the two-word threshold.

Vapi estimates transcription-based interruption at roughly 200–500 ms, compared with 50–100 ms for VAD. See [Vapi stop-speaking options](https://docs.vapi.ai/customization/voice-pipeline-configuration).

### 3. Alternatively, retain VAD and raise `voiceSeconds`

If language-independent and very fast interruption is essential, retain `numWords: 0` but test `voiceSeconds` around 0.35–0.4 rather than 0.2.

This can filter brief noises and very short “mm” responses, but cannot distinguish a sustained “yeah” from “stop.” `voiceSeconds` only applies when `numWords` is zero; it is not an additional threshold when transcription-based interruption is active.

The VAD-duration and word-threshold options should be A/B tested as separate policies.

### 4. Fix noise, competing speech, and echo

Before selecting an architecture, label whether each observed interruption was caused by:

- The customer speaking.
- Another nearby person.
- Television, music, or radio.
- The assistant's TTS returning through speakerphone.
- A breath, cough, or impact sound.

Vapi's Krisp-based smart denoising is disabled by default and is its recommended first noise-control option. Fourier denoising is experimental and may distort speech. See [Vapi background speech denoising](https://docs.vapi.ai/documentation/assistants/conversation-behavior/background-speech-denoising).

For browser calls, verify acoustic echo cancellation and noise suppression. For telephone calls, separately test handset, speakerphone, car Bluetooth, and noisy-room conditions.

Replacing the VAD with WebRTC VAD, Silero, or TEN VAD may improve speech/no-speech accuracy but does not resolve intent. A conventional VAD still answers only “is this speech?” Silero supports streaming at 8/16 kHz, while TEN provides lightweight frame-level detection. See [Silero VAD](https://github.com/snakers4/silero-vad) and [TEN VAD](https://github.com/TEN-framework/ten-vad).

### 5. Reduce the opportunity for overlap

Conversation design can materially improve a half-duplex system:

- Keep answers to one or two short ideas.
- Ask one question at a time.
- Put optional detail behind “Would you like me to explain that?”
- Avoid long lists without checkpoints.
- Use short pauses at semantic boundaries.
- Do not repeat information the caller already confirmed.

Shorter turns reduce both the probability and cost of interruption. A push-to-talk or explicit “tap to interrupt” control is a less natural but deterministic fallback for noisy web/mobile environments.

### 6. Improve endpointing inside Vapi

Options for deciding when the customer has finished speaking include:

- LiveKit smart endpointing for English.
- Deepgram Flux conversational end-of-turn events, including multilingual support.
- AssemblyAI semantic plus acoustic end-of-turn detection.
- A custom endpointing server called by Vapi.

[Deepgram Flux](https://developers.deepgram.com/docs/flux/quickstart) and [AssemblyAI turn detection](https://www.assemblyai.com/docs/universal-streaming/turn-detection) can prevent replies during thinking pauses. These improve the opposite boundary—when the assistant starts—rather than directly fixing backchannel classification while it is already speaking.

### 7. Pilot native audio within Vapi

Vapi has two relevant native-audio paths:

- [OpenAI Realtime in Vapi](https://docs.vapi.ai/openai-realtime): native audio-in/audio-out rather than separate STT, LLM, and TTS services.
- [Vapi GPT-Live beta](https://docs.vapi.ai/gpt-live/overview): keeps listening while speaking and can incorporate interruptions or additional details while background reasoning continues.

GPT-Live is the most direct sophisticated option without leaving Vapi. Classic start/stop-speaking plans do not configure a GPT-Live conversation, however, and Vapi currently documents constraints around voices, squads, warm transfers, exact prerecorded speech, knowledge bases, and some tools. Review [GPT-Live compatibility](https://docs.vapi.ai/gpt-live/configuration) before a migration.

A limited traffic-split pilot is preferable to an immediate migration.

### 8. Use a system with semantic interruption handling

Two managed/framework options stand out:

- [LiveKit Agents interruption tuning](https://docs.livekit.io/agents/logic/turns/tuning/) supports adaptive interruption mode, word/duration thresholds, false-interruption detection, and resuming speech after a false positive. It explicitly distinguishes genuine interruption from backchanneling.
- [AssemblyAI's Voice Agent API](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/turn-detection-and-interruptions) classifies backchannels versus true interruptions semantically and adapts timing to the caller.

Pipecat is a more composable alternative: combine Silero or TEN VAD, streaming STT, its local Smart Turn model, and a custom interruption policy. Its [Smart Turn model](https://github.com/pipecat-ai/docs/blob/main/api-reference/server/utilities/turn-detection/smart-turn-overview.mdx) primarily handles end-of-turn, so interruption intent still needs a separate strategy.

### 9. Build a two-stage “soft interruption” controller

A sophisticated chained system should avoid immediately destroying the response on first speech onset:

1. VAD detects possible speech.
2. Duck or pause assistant audio without flushing it.
3. Gather approximately 300–700 ms of audio and partial transcription.
4. Classify it as backchannel, takeover, correction, noise, or uncertain.
5. Resume after a backchannel; flush after a genuine takeover.
6. Fast-path explicit commands such as “stop.”
7. Trim conversation history to only the assistant speech the caller actually heard.

This is substantially better than a single threshold, but requires precise playback-buffer, cancellation, timing, and history management. LiveKit's adaptive and false-interruption-resume features approximate this design.

### 10. Redesign around native full duplex

A true full-duplex model jointly processes incoming and outgoing audio and can decide whether to continue, adapt, or yield instead of applying an external binary cutoff.

Options include:

- Vapi GPT-Live.
- Direct OpenAI GPT-Live/Realtime.
- [Gemini Live](https://ai.google.dev/gemini-api/docs/live-api/capabilities), with continuous audio, configurable activity detection, and interruption signals.
- Self-hosted research systems such as [Moshi](https://kyutai.org/Moshi.pdf), which models both audio streams simultaneously.

Native-audio systems can be more natural but provide less inspectable intermediate text and may complicate policy enforcement, exact wording, voice/provider choice, tool orchestration, and cost control. Open full-duplex models should currently be treated as R&D rather than the first production fix.

Hume EVI was considered but should not be the target of a new migration: its documentation says the EVI API will shut down on November 13, 2026. See the [Hume EVI notice](https://dev.hume.ai/voice/docs/speech-to-speech-evi/overview).

## Recommended evaluation sequence

1. Review 50–100 affected recordings and label each event as backchannel, true takeover, noise, competing speaker, or echo.
2. A/B test two classic Vapi policies:
   - `numWords: 2` plus acknowledgement and explicit interruption phrases.
   - `numWords: 0` plus `voiceSeconds: 0.35–0.4`.
3. Enable Krisp smart denoising if acoustics contribute.
4. Shorten assistant turns.
5. Add semantic endpointing for customer pauses.
6. Pilot Vapi GPT-Live against the tuned classic pipeline.
7. Build or migrate to a custom LiveKit/Pipecat controller only if managed options fail the feature or accuracy requirements.

Track:

- False-interruption rate.
- Missed-interruption rate.
- Time-to-yield for urgent commands such as “stop.”
- False-interruption recovery/resume correctness.
- End-of-turn response latency.
- Customer cut-off rate.
- Task completion and transfer/escalation rate.

## Information needed before choosing a configuration

- Call channel: PSTN, SIP, browser WebRTC, mobile, or a mixture.
- Languages and code-switching requirements.
- Current `stopSpeakingPlan` and transcriber configuration.
- Real examples of words that cause unwanted cutoffs.
- Whether recordings show noise, competing speech, or echo.
- Whether exact wording, custom voices, squads, knowledge bases, warm transfers, or particular tools are required.

## Proposed experiment at the time

The research proposed comparing two settings:

1. Transcription-based interruption with `numWords: 2`, curated acknowledgement phrases, and urgent one-word interruption phrases.
2. The current VAD path with a more conservative `voiceSeconds` threshold.

The result should be evaluated on real calls before considering a GPT-Live or external-pipeline migration.
