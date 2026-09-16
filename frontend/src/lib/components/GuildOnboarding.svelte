<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { goto } from '$app/navigation';
  import { api, userErrorMessage } from '$lib/api/client';
  import { entityRef } from '$lib/chat/refs';
  import type { Guild } from '$lib/chat/types';
  import { emptyOnboarding, type OnboardingResponse } from '$lib/chat/onboarding';
  import { resolve } from '$app/paths';
  import { assetUrl } from '$lib/media/assets';
  import Icon from './Icon.svelte';

  let {
    guild,
    admin = false,
    onComplete,
    onChannels
  }: {
    guild: Guild;
    admin?: boolean;
    onComplete?: () => void;
    onChannels?: (channels: string[] | null) => void;
  } = $props();
  let response = $state<OnboardingResponse | null>(null);
  let config = $state(emptyOnboarding());
  let answers = $state<Record<string, string[]>>({});
  let completedTasks = $state<string[]>([]);
  let extraChannels = $state<string[]>([]);
  let accepted = $state(false);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');
  let dialog = $state<HTMLDialogElement>();
  let preview = $state(false);
  let step = $state(0);
  let open = $state(false);
  let generation = 0;
  let editorSection = $state(0);
  const editorSections = ['Server rules', 'Default channels', 'Questions', 'Server guide'];
  const returning = $derived(!!response?.state.completed_at && !response?.needs_rules);
  const tasks = $derived(config.guide.filter((item) => item.kind === 'task'));
  const finishedTasks = $derived(tasks.filter((item) => completedTasks.includes(item.id)).length);
  const channels = $derived(
    (guild.channels ?? []).filter((channel) => ![4, 10, 11, 12].includes(channel.type))
  );
  const roles = $derived(
    (guild.roles ?? []).filter((role) => role.id !== guild.id && !role.managed)
  );
  const questions = $derived(
    config.questions.filter(
      (question) => !response?.needs_onboarding || question.before_join || preview
    )
  );
  const steps = $derived([
    'Welcome',
    ...(config.rules.length ? ['Server rules'] : []),
    ...questions.map((question) => question.title),
    'Server guide'
  ]);
  const questionIndex = $derived(step - 1 - (config.rules.length ? 1 : 0));
  const question = $derived(questions[questionIndex]);
  const canContinue = $derived(
    step === 1 && config.rules.length
      ? accepted
      : !question?.required || (answers[question.id]?.length ?? 0) > 0
  );
  const path = $derived(`/guilds/${entityRef(guild)}/onboarding`);

  $effect(() => {
    const ref = entityRef(guild);
    // Recheck after guild policy updates, including newly published rules.
    const version = guild.permission_generation;
    void version;
    const current = ++generation;
    loading = true;
    untrack(() => onChannels?.(null));
    void api<OnboardingResponse>(`/guilds/${ref}/onboarding`)
      .then(async (value) => {
        if (current !== generation) return;
        response = value;
        onChannels?.(
          value.config.enabled && value.state.completed_at
            ? (value.state.channel_ids ?? null)
            : null
        );
        config = structuredClone(value.config);
        answers = value.state.answers ?? {};
        completedTasks = value.state.completed_tasks ?? [];
        extraChannels = value.state.extra_channel_ids ?? [];
        accepted = !value.needs_rules;
        if (!admin && (value.needs_rules || value.needs_onboarding)) await show(false);
      })
      .catch((caught) => {
        if (current === generation)
          error = userErrorMessage(caught, 'Could not load server onboarding.');
      })
      .finally(() => {
        if (current === generation) loading = false;
      });
  });

  async function show(isPreview: boolean, start = 0) {
    preview = isPreview;
    step = start;
    open = true;
    if (isPreview) {
      answers = {};
      accepted = false;
      completedTasks = [];
    }
    await tick();
    dialog?.showModal();
  }
  function close() {
    dialog?.close();
    open = false;
  }
  function choose(id: string) {
    if (!question) return;
    const existing = answers[question.id] ?? [];
    answers = {
      ...answers,
      [question.id]: question.multiple
        ? existing.includes(id)
          ? existing.filter((value) => value !== id)
          : [...existing, id]
        : existing.includes(id) && !question.required
          ? []
          : [id]
    };
  }
  async function saveConfig() {
    busy = true;
    error = '';
    notice = '';
    try {
      response = await api<OnboardingResponse>(path, {
        method: 'PUT',
        body: JSON.stringify(config)
      });
      config = structuredClone($state.snapshot(response.config));
      notice = 'Server onboarding saved.';
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save onboarding.');
    } finally {
      busy = false;
    }
  }
  async function finish() {
    if (preview) {
      close();
      return;
    }
    busy = true;
    error = '';
    try {
      response = await api<OnboardingResponse>(`${path}/@me`, {
        method: 'PUT',
        body: JSON.stringify({
          revision: config.revision,
          accept_rules: accepted,
          answers,
          completed_tasks: completedTasks,
          extra_channel_ids: extraChannels
        })
      });
      close();
      onChannels?.(response.state.channel_ids ?? null);
      notice = 'You’re all set. Welcome in!';
      onComplete?.();
    } catch (caught) {
      error = userErrorMessage(caught, 'Could not save your choices.');
    } finally {
      busy = false;
    }
  }
  function addQuestion() {
    config.questions.push({
      id: crypto.randomUUID(),
      title: '',
      description: '',
      required: false,
      multiple: false,
      before_join: true,
      options: [
        {
          id: crypto.randomUUID(),
          title: '',
          description: '',
          emoji: '',
          role_ids: [],
          channel_ids: []
        }
      ]
    });
  }
</script>

{#if admin}
  <section id="onboarding" class="onboarding-settings">
    <div class="section-heading">
      <div>
        <p class="eyebrow">COMMUNITY</p>
        <h2>Rules &amp; onboarding</h2>
        <p>Give new members a warm welcome and a clear place to start.</p>
      </div>
      <button disabled={loading} onclick={() => show(true)}>Preview as a member</button>
    </div>
    {#if loading}<p role="status">Loading onboarding…</p>{:else if response?.can_manage}
      <label class="enable"
        ><span
          ><strong>Enable server onboarding</strong><small
            >Members review rules and choose their channels and roles.</small
          ></span
        ><input type="checkbox" role="switch" bind:checked={config.enabled} /></label
      >
      <nav class="setup-nav" aria-label="Onboarding setup sections">
        {#each editorSections as title, index (title)}<button
            class:active={editorSection === index}
            aria-current={editorSection === index ? 'step' : undefined}
            onclick={() => (editorSection = index)}><span>{index + 1}</span>{title}</button
          >{/each}
      </nav>
      <div class="editor-section" hidden={editorSection !== 0}>
        <h3>Server rules</h3>
        <p>
          Members must accept these before they can participate. Changing rules requires acceptance
          again.
        </p>
        {#each config.rules as rule, index (index)}<div class="rule-editor">
            <span>{index + 1}</span><textarea
              aria-label={`Rule ${index + 1}`}
              value={rule}
              oninput={(event) => (config.rules[index] = event.currentTarget.value)}
              maxlength="1000"
              rows="2"
            ></textarea><button
              aria-label={`Remove rule ${index + 1}`}
              onclick={() => config.rules.splice(index, 1)}>×</button
            >
          </div>{/each}
        <button disabled={config.rules.length >= 16} onclick={() => config.rules.push('')}
          >+ Add a rule</button
        >
      </div>
      <div class="editor-section" hidden={editorSection !== 1}>
        <h3>Default channels</h3>
        <p>Choose the places every new member should start with.</p>
        <div class="channel-choices">
          {#each channels as channel (entityRef(channel))}<label class="check"
              ><input
                type="checkbox"
                value={entityRef(channel)}
                bind:group={config.default_channel_ids}
              /># {channel.name}</label
            >{/each}
        </div>
      </div>
      <div class="editor-section" hidden={editorSection !== 2}>
        <h3>Customization questions</h3>
        <p>
          Let people choose their interests. Answer choices can add channels and participation
          roles.
        </p>
        {#each config.questions as item, index (item.id)}
          <article class="question-editor">
            <header>
              <strong>Question {index + 1}</strong>
              <div>
                <button
                  disabled={index === 0}
                  aria-label="Move question up"
                  onclick={() => {
                    [config.questions[index - 1], config.questions[index]] = [
                      config.questions[index],
                      config.questions[index - 1]
                    ];
                  }}>↑</button
                ><button
                  aria-label="Remove question"
                  onclick={() => config.questions.splice(index, 1)}>Remove</button
                >
              </div>
            </header>
            <label
              >Question<input
                bind:value={item.title}
                maxlength="200"
                placeholder="What brings you here?"
              /></label
            ><label
              >Description<input
                bind:value={item.description}
                maxlength="500"
                placeholder="Choose the things you enjoy."
              /></label
            >
            <div class="flags">
              <label class="check"
                ><input type="checkbox" bind:checked={item.required} />Required</label
              ><label class="check"
                ><input type="checkbox" bind:checked={item.multiple} />Allow multiple answers</label
              ><label class="check"
                ><input type="checkbox" bind:checked={item.before_join} />Ask new members</label
              >
            </div>
            {#each item.options as option, optionIndex (option.id)}
              <details class="option-editor" open>
                <summary>{option.title || `Answer ${optionIndex + 1}`}</summary>
                <div class="option-fields">
                  <label
                    >Emoji<input bind:value={option.emoji} maxlength="16" placeholder="🎮" /></label
                  ><label
                    >Answer<input
                      bind:value={option.title}
                      maxlength="100"
                      placeholder="Gaming"
                    /></label
                  >
                </div>
                <label>Description<input bind:value={option.description} maxlength="300" /></label>
                <div class="assignment-fields">
                  <details class="assignment-picker">
                    <summary
                      ><Icon name="hash" size={16} />{option.channel_ids.length
                        ? `${option.channel_ids.length} channels selected`
                        : 'Choose channels'}<Icon name="chevron-down" size={14} /></summary
                    >
                    <div class="assignment-options">
                      {#each channels as channel (entityRef(channel))}<label
                          ><input
                            type="checkbox"
                            value={entityRef(channel)}
                            bind:group={option.channel_ids}
                          /># {channel.name}</label
                        >{/each}
                    </div>
                  </details>
                  <details class="assignment-picker">
                    <summary
                      ><Icon name="users" size={16} />{option.role_ids.length
                        ? `${option.role_ids.length} roles selected`
                        : 'Choose roles'}<Icon name="chevron-down" size={14} /></summary
                    >
                    <div class="assignment-options">
                      {#each roles as role (entityRef(role))}<label
                          ><input
                            type="checkbox"
                            value={entityRef(role)}
                            bind:group={option.role_ids}
                          />{role.name}</label
                        >{:else}<small>No roles available</small>{/each}
                    </div>
                  </details>
                </div>
                <small
                  >Only roles without moderation or administration permissions can be self-selected.</small
                >
                <button
                  disabled={item.options.length <= 1}
                  onclick={() => item.options.splice(optionIndex, 1)}>Remove answer</button
                >
              </details>
            {/each}
            <button
              disabled={item.options.length >= 20}
              onclick={() =>
                item.options.push({
                  id: crypto.randomUUID(),
                  title: '',
                  description: '',
                  emoji: '',
                  role_ids: [],
                  channel_ids: []
                })}>+ Add an answer</button
            >
          </article>
        {/each}
        <button disabled={config.questions.length >= 12} onclick={addQuestion}
          >+ Add a question</button
        >
      </div>
      <div class="editor-section" hidden={editorSection !== 3}>
        <h3>Server guide</h3>
        <label
          >Welcome message<textarea bind:value={config.welcome} maxlength="1000" rows="3"
          ></textarea></label
        >

        <p>Suggest a few first steps and useful resources.</p>
        {#each config.guide as item, index (item.id)}<article class="question-editor">
            <label
              >Title<input
                bind:value={item.title}
                maxlength="100"
                placeholder="Introduce yourself"
              /></label
            ><label>Description<input bind:value={item.description} maxlength="300" /></label>
            <div class="option-fields">
              <label
                >Type<select bind:value={item.kind}
                  ><option value="task">New member task</option><option value="resource"
                    >Resource</option
                  ></select
                ></label
              ><label
                >Channel<select bind:value={item.channel_id}
                  ><option value={null}>No channel</option
                  >{#each channels as channel (entityRef(channel))}<option
                      value={entityRef(channel)}># {channel.name}</option
                    >{/each}</select
                ></label
              >
            </div>
            <button onclick={() => config.guide.splice(index, 1)}>Remove</button>
          </article>{/each}
        <button
          disabled={config.guide.length >= 20}
          onclick={() =>
            config.guide.push({
              id: crypto.randomUUID(),
              title: '',
              description: '',
              channel_id: null,
              kind: 'task'
            })}>+ Add a guide item</button
        >
      </div>
      <div class="save-row">
        <span>Preview your welcome before publishing.</span><button
          class="primary"
          disabled={busy}
          onclick={saveConfig}>{busy ? 'Saving…' : 'Save onboarding'}</button
        >
      </div>
    {:else}<p>You need Manage Server and Manage Roles permissions to configure onboarding.</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}{#if notice}<p role="status">
        {notice}
      </p>{/if}
  </section>
{:else if response?.config.enabled}
  <button class="onboarding-entry" onclick={() => show(false, returning ? steps.length - 1 : 0)}
    ><Icon name="sparkles" size={20} /><span
      ><strong
        >{response.needs_rules || response.needs_onboarding
          ? 'Finish joining this server'
          : 'Server Guide'}</strong
      ><small
        >{response.needs_rules
          ? 'Read and accept the server rules'
          : 'Your welcome, tasks and resources'}</small
      ></span
    ><span aria-hidden="true">›</span></button
  >
  {#if returning}<button
      class="onboarding-entry secondary-entry"
      onclick={() => show(false, 1 + (config.rules.length ? 1 : 0))}
      ><Icon name="users" size={20} /><span
        ><strong>Channels &amp; Roles</strong><small>Customize your server</small></span
      ><Icon name="chevron-right" size={16} /></button
    >{/if}
{:else if error}<p class="error" role="alert">{error}</p>{/if}

{#if open}
  <dialog
    bind:this={dialog}
    class="onboarding-dialog"
    onclose={() => (open = false)}
    aria-labelledby="welcome-title"
  >
    <aside class="welcome-sidebar">
      <div class="server-cover">
        {#if guild.banner_hash}<img
            src={assetUrl(guild.banner_hash, 'original', guild)}
            alt=""
          />{:else}<Icon name="sparkles" size={64} />{/if}
      </div>
      <div class="guild-emblem">
        {#if guild.icon_hash}<img
            src={assetUrl(guild.icon_hash, 'thumbnail_128', guild)}
            alt=""
          />{:else}{guild.name.slice(0, 2)}{/if}
      </div>
      <h2>{guild.name}</h2>
      <p>{preview ? 'Member preview' : 'A place for you'}</p>
      <ol>
        {#each steps as title, index (index)}<li
            class:current={step === index}
            class:done={step > index}
          >
            <button
              disabled={!returning || preview}
              aria-current={step === index ? 'step' : undefined}
              onclick={() => (step = index)}
              ><span>{step > index ? '✓' : index + 1}</span>{title}</button
            >
          </li>{/each}
      </ol>
    </aside>
    <div class="welcome-main">
      <header>
        <span class="eyebrow">{preview ? 'PREVIEW' : `STEP ${step + 1} OF ${steps.length}`}</span
        ><button class="close" aria-label="Close onboarding" onclick={close}>×</button>
      </header>
      <progress
        class="step-progress"
        value={step + 1}
        max={steps.length}
        aria-label="Onboarding progress"
      ></progress>
      <div class="welcome-content">
        {#if step === 0}<span class="hero-emoji" aria-hidden="true"
            ><Icon name="users" size={48} /></span
          >
          <h1 id="welcome-title">Welcome to {guild.name}</h1>
          <p class="welcome-message">{config.welcome}</p>
          <div class="welcome-promise">
            <Icon name="users" size={24} />
            <div>
              <strong>Make this place yours</strong>
              <p>Review the rules, choose your interests, and find your first conversation.</p>
            </div>
          </div>
        {:else if config.rules.length && step === 1}<span class="hero-emoji" aria-hidden="true"
            >📋</span
          >
          <h1 id="welcome-title">A few ground rules</h1>
          <p>Help keep {guild.name} welcoming for everyone.</p>
          <ol class="rules">
            {#each config.rules as rule, index (index)}<li>{rule}</li>{/each}
          </ol>
          <label class="agreement"
            ><input type="checkbox" bind:checked={accepted} /><span
              >I have read and agree to the server rules.</span
            ></label
          >
        {:else if question}<h1 id="welcome-title">{question.title || 'Your question'}</h1>
          <p>{question.description}</p>
          <p class="hint">
            {question.multiple ? 'Choose all that apply' : 'Choose one'} · {question.required
              ? 'Required'
              : 'Optional'}
          </p>
          <div class="answer-grid">
            {#each question.options as option (option.id)}<button
                class:selected={answers[question.id]?.includes(option.id)}
                class="answer-card"
                aria-pressed={answers[question.id]?.includes(option.id) ?? false}
                onclick={() => choose(option.id)}
                ><span class="answer-emoji"
                  >{#if option.emoji}{option.emoji}{:else}<Icon name="users" size={28} />{/if}</span
                ><strong>{option.title || 'Answer choice'}</strong>{#if option.description}<small
                    >{option.description}</small
                  >{/if}<span
                  class="selection-mark"
                  class:checked={answers[question.id]?.includes(option.id)}
                  class:radio={!question.multiple}
                  aria-hidden="true"
                  >{#if answers[question.id]?.includes(option.id)}<Icon
                      name="check"
                      size={14}
                    />{/if}</span
                ></button
              >{/each}
          </div>
        {:else}<span class="hero-emoji" aria-hidden="true"><Icon name="sparkles" size={48} /></span>
          <h1 id="welcome-title">Server Guide</h1>
          <p>Here are a few good places to start. You can revisit this guide anytime.</p>
          {#if tasks.length}<div class="task-progress">
              <strong>Get started</strong><span>{finishedTasks} of {tasks.length} complete</span
              ><progress value={finishedTasks} max={tasks.length} aria-label="New member tasks"
              ></progress>
            </div>{/if}
          <div class="guide-list">
            {#each config.guide as item (item.id)}<article>
                <div>
                  {#if item.kind === 'task'}<input
                      type="checkbox"
                      aria-label={`Mark ${item.title} complete`}
                      value={item.id}
                      bind:group={completedTasks}
                    />{:else}<Icon name="hash" size={22} />{/if}<span
                    ><small class="resource-kind"
                      >{item.kind === 'task' ? 'NEW MEMBER TASK' : 'RESOURCE'}</small
                    ><strong>{item.title}</strong><small>{item.description}</small></span
                  >
                </div>
                {#if item.channel_id && channels.find((channel) => entityRef(channel) === item.channel_id)}{@const channel =
                    channels.find((channel) => entityRef(channel) === item.channel_id)!}<a
                    href={resolve(
                      `/g/${encodeURIComponent(entityRef(guild))}/${encodeURIComponent(entityRef(channel))}`
                    )}
                    onclick={async (event) => {
                      event.preventDefault();
                      if (preview) return;
                      await finish();
                      if (!error)
                        await goto(
                          resolve(
                            `/g/${encodeURIComponent(entityRef(guild))}/${encodeURIComponent(entityRef(channel))}`
                          )
                        );
                    }}>Open channel →</a
                  >{/if}
              </article>{/each}
          </div>
          <details class="browse-channels">
            <summary>Browse more channels</summary>
            <p>Add other places you would like to follow.</p>
            <div class="channel-choices">
              {#each channels as channel (entityRef(channel))}<label class="check"
                  ><input type="checkbox" value={entityRef(channel)} bind:group={extraChannels} /># {channel.name}</label
                >{/each}
            </div>
          </details>
          {#if !config.guide.length}<p>
              Say hello in a channel that catches your eye.
            </p>{/if}{#if config.default_channel_ids.length}<h3>Your starting channels</h3>
            <div class="channel-choices">
              {#each channels.filter( (channel) => config.default_channel_ids.includes(entityRef(channel)) ) as channel (entityRef(channel))}<span
                  class="channel-pill"># {channel.name}</span
                >{/each}
            </div>{/if}{/if}
        {#if error}<p class="error" role="alert">{error}</p>{/if}
      </div>
      <footer>
        <button disabled={step === 0 || busy} onclick={() => (step -= 1)}>Back</button><span
          >{step + 1} / {steps.length}</span
        >{#if step < steps.length - 1}<button
            class="primary"
            disabled={!canContinue}
            onclick={() => (step += 1)}>Continue →</button
          >{:else}<button class="primary" disabled={busy} onclick={finish}
            >{busy
              ? 'Saving…'
              : preview
                ? 'Finish preview'
                : returning
                  ? 'Save changes'
                  : 'Save & enter server'}</button
          >{/if}
      </footer>
    </div>
  </dialog>
{/if}

<style>
  .onboarding-settings {
    padding: 24px;
    margin: 20px 0;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--surface);
  }
  .section-heading,
  .save-row,
  .question-editor header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
  }
  h1,
  h2,
  h3,
  p {
    margin: 0 0 12px;
  }
  p,
  small {
    color: var(--text-muted);
    line-height: 1.6;
  }
  h1 {
    font-size: clamp(1.6rem, 3vw, 2.3rem);
    line-height: 1.2;
  }
  .eyebrow {
    color: var(--accent);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.1em;
  }
  label {
    display: grid;
    gap: 7px;
    margin: 14px 0;
  }
  input:not([type='checkbox']),
  textarea,
  select {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 12px;
    background: var(--surface-raised);
    color: var(--text);
    font: inherit;
  }
  input[type='checkbox'] {
    width: 18px;
    height: 18px;
    accent-color: var(--accent);
    flex-shrink: 0;
  }
  button,
  a {
    font: inherit;
  }
  button {
    padding: 10px 14px;
    border: 1px solid var(--line);
    border-radius: 9px;
    background: var(--surface-raised);
    color: var(--text);
    cursor: pointer;
  }
  button:hover {
    border-color: var(--accent);
  }
  button:disabled {
    opacity: 0.45;
    cursor: default;
  }
  button.primary {
    background: var(--accent);
    color: var(--on-accent, #fff);
    border-color: transparent;
    font-weight: 700;
  }
  button:focus-visible,
  a:focus-visible {
    outline: 3px solid var(--accent);
    outline-offset: 3px;
  }
  .enable,
  .agreement {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 18px;
    border: 1px solid var(--line);
    border-radius: 12px;
  }
  .enable {
    justify-content: space-between;
  }
  .enable small {
    display: block;
    margin-top: 4px;
  }
  .editor-section[hidden] {
    display: none;
  }
  .editor-section {
    border-top: 1px solid var(--line);
    padding: 24px 0;
  }
  .rule-editor {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .rule-editor textarea {
    flex: 1;
  }
  .flags,
  .channel-choices {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0;
  }
  .question-editor {
    padding: 18px;
    margin: 14px 0;
    background: var(--surface-subtle);
    border: 1px solid var(--line);
    border-radius: 12px;
  }
  .option-editor {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 14px;
    margin: 12px 0;
  }
  summary {
    cursor: pointer;
    font-weight: 700;
  }
  .option-fields {
    display: grid;
    grid-template-columns: 1fr 2fr;
    gap: 12px;
  }
  .save-row {
    position: sticky;
    bottom: 0;
    padding: 16px 0;
    background: var(--surface);
  }
  .onboarding-entry {
    width: calc(100% - 16px);
    margin: 8px;
    display: flex;
    align-items: center;
    gap: 12px;
    text-align: left;
    background: color-mix(in srgb, var(--accent) 10%, var(--surface));
  }
  .onboarding-entry span:nth-child(2) {
    flex: 1;
  }
  .onboarding-entry strong,
  .onboarding-entry small {
    display: block;
    font-size: 12px;
  }
  .onboarding-dialog {
    padding: 0;
    width: min(920px, calc(100vw - 40px));
    max-height: min(760px, calc(100dvh - 40px));
    border: 1px solid var(--line);
    border-radius: 20px;
    color: var(--text);
    background: var(--surface);
    overflow: hidden;
    box-shadow: 0 30px 100px #0008;
  }
  .onboarding-dialog[open] {
    display: grid;
    grid-template-columns: 250px 1fr;
  }
  .onboarding-dialog::backdrop {
    background: #000a;
    backdrop-filter: blur(4px);
  }
  .welcome-sidebar {
    padding: 0 24px 28px;
    background: linear-gradient(
      150deg,
      color-mix(in srgb, var(--accent) 22%, var(--surface)),
      var(--surface-subtle)
    );
  }
  .guild-emblem {
    width: 64px;
    height: 64px;
    display: grid;
    place-items: center;
    border-radius: 20px;
    background: var(--accent);
    color: white;
    font-size: 24px;
    font-weight: 800;
    margin: -28px 0 20px;
    position: relative;
    border: 5px solid var(--surface);
  }
  .guild-emblem img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: inherit;
  }
  .welcome-sidebar ol {
    list-style: none;
    padding: 0;
    display: grid;
    gap: 18px;
    margin-top: 24px;
  }
  .welcome-sidebar li {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .welcome-sidebar li span {
    display: grid;
    place-items: center;
    border: 1px solid var(--line);
    min-width: 26px;
    height: 26px;
    border-radius: 50%;
  }
  .welcome-sidebar li.current {
    color: var(--text);
    font-weight: 700;
  }
  .current span,
  .done span {
    background: var(--accent);
    color: white;
  }
  .welcome-main {
    display: flex;
    flex-direction: column;
    min-width: 0;
    max-height: min(760px, calc(100dvh - 40px));
  }
  .welcome-main header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 28px 0;
  }
  .close {
    border: 0;
    background: transparent;
    font-size: 24px;
    padding: 0 8px;
  }
  .welcome-content {
    flex: 1;
    min-height: 240px;
    overflow-y: auto;
    padding: 28px;
  }
  .hero-emoji {
    display: block;
    font-size: 48px;
    margin-bottom: 20px;
  }
  .welcome-message {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .welcome-promise {
    display: flex;
    gap: 16px;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 20px;
    margin-top: 24px;
  }
  .welcome-promise p {
    margin: 6px 0 0;
  }
  .rules {
    padding-left: 26px;
  }
  .rules li {
    padding: 12px 0 12px 8px;
    border-bottom: 1px solid var(--line);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .agreement {
    background: var(--surface-raised);
  }
  .answer-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .answer-card {
    position: relative;
    display: flex;
    flex-direction: column;
    text-align: left;
    gap: 8px;
    padding: 20px;
    min-height: 140px;
    border-width: 2px;
  }
  .answer-card.selected {
    border-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, var(--surface));
  }
  .answer-emoji {
    font-size: 30px;
  }
  .selection-mark {
    position: absolute;
    right: 14px;
    top: 12px;
    color: white;
    width: 20px;
    height: 20px;
    border: 2px solid var(--line);
    border-radius: 5px;
    display: grid;
    place-items: center;
  }
  .selection-mark.checked {
    background: var(--accent);
    border-color: var(--accent);
  }
  .selection-mark.radio {
    border-radius: 50%;
  }
  .hint {
    font-size: 12px;
  }
  .guide-list {
    display: grid;
    gap: 12px;
  }
  .guide-list article {
    border: 1px solid var(--line);
    padding: 16px;
    border-radius: 12px;
  }
  .guide-list article > div {
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .guide-list small {
    display: block;
  }
  .guide-list a {
    display: inline-block;
    margin-top: 10px;
    color: var(--accent);
  }
  .channel-pill {
    background: var(--surface-raised);
    padding: 8px 12px;
    border-radius: 8px;
  }
  footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 20px 28px;
    border-top: 1px solid var(--line);
  }
  footer > span {
    font-size: 12px;
    color: var(--text-muted);
  }
  .error {
    color: var(--danger);
  }
  .enable input[role='switch'] {
    appearance: none;
    width: 40px;
    height: 24px;
    border-radius: 16px;
    padding: 3px;
    background: var(--line);
    cursor: pointer;
    margin: 0;
  }
  .enable input[role='switch']::before {
    content: '';
    display: block;
    width: 18px;
    height: 18px;
    background: white;
    border-radius: 50%;
    box-shadow: 0 1px 3px #0003;
    transition: transform 0.15s ease;
  }
  .enable input[role='switch']:checked {
    background: var(--accent);
  }
  .enable input[role='switch']:checked::before {
    transform: translateX(16px);
  }
  .enable input[role='switch']:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }
  .assignment-fields {
    display: grid;
    gap: 8px;
    margin: 12px 0;
  }
  .assignment-picker {
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface);
  }
  .assignment-picker summary {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 12px;
    cursor: pointer;
    list-style: none;
    font-size: 13px;
  }
  .assignment-picker summary::-webkit-details-marker {
    display: none;
  }
  .assignment-picker summary :global(svg:last-child) {
    margin-left: auto;
  }
  .assignment-options {
    max-height: 200px;
    overflow-y: auto;
    padding: 0 8px 8px;
    border-top: 1px solid var(--line);
  }
  .assignment-options label {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    padding: 10px 6px;
    font-size: 13px;
  }
  .assignment-options label:hover {
    background: var(--surface-hover);
    border-radius: 6px;
  }
  .assignment-options input {
    margin: 0;
  }
  .setup-nav {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
    margin: 24px 0 0;
    padding: 6px;
    background: var(--surface-subtle);
    border: 1px solid var(--line);
    border-radius: 12px;
  }
  .setup-nav button {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 0;
    background: transparent;
    padding: 10px;
    font-size: 12px;
    text-align: left;
  }
  .setup-nav button.active {
    background: var(--surface-raised);
    color: var(--accent);
    box-shadow: 0 1px 4px #0001;
  }
  .setup-nav button span {
    width: 22px;
    height: 22px;
    display: grid;
    place-items: center;
    border-radius: 50%;
    border: 1px solid var(--line);
    flex-shrink: 0;
  }
  .setup-nav button.active span {
    background: var(--accent);
    color: white;
    border-color: transparent;
  }
  .server-cover {
    height: 132px;
    margin: 0 -24px;
    display: grid;
    place-items: center;
    overflow: hidden;
    color: color-mix(in srgb, var(--accent) 65%, white);
    background:
      radial-gradient(
        ellipse at top right,
        color-mix(in srgb, var(--accent) 40%, transparent),
        transparent
      ),
      var(--surface-subtle);
  }
  .server-cover img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .welcome-sidebar li button {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    text-align: left;
    font-size: 12px;
    line-height: 1.4;
  }
  .welcome-sidebar li button:disabled {
    opacity: 1;
    cursor: default;
  }
  .step-progress {
    flex-shrink: 0;
    width: calc(100% - 56px);
    margin: 16px 28px 0;
  }
  progress {
    height: 4px;
    appearance: none;
    border: 0;
    border-radius: 8px;
    overflow: hidden;
    background: var(--line);
    accent-color: var(--accent);
  }
  progress::-webkit-progress-bar {
    background: var(--line);
  }
  progress::-webkit-progress-value {
    background: var(--accent);
  }
  progress::-moz-progress-bar {
    background: var(--accent);
  }
  .task-progress {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin: 24px 0 18px;
  }
  .task-progress span {
    color: var(--text-muted);
    font-size: 12px;
  }
  .task-progress progress {
    width: 100%;
  }
  .resource-kind {
    font-size: 10px;
    letter-spacing: 0.07em;
    font-weight: 700;
    margin-bottom: 4px;
  }
  .secondary-entry {
    background: transparent;
    border-color: transparent;
    margin-top: 0;
  }
  .secondary-entry small {
    display: none;
  }
  .hero-emoji {
    width: 64px;
    height: 64px;
    display: grid;
    place-items: center;
    border-radius: 18px;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent) 12%, transparent);
  }
  @media (max-width: 650px) {
    .setup-nav {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .server-cover {
      display: none;
    }
    .step-progress {
      width: calc(100% - 40px);
      margin-left: 20px;
      margin-right: 20px;
    }
    .onboarding-dialog {
      width: calc(100vw - 20px);
      max-height: calc(100dvh - 20px);
    }
    .onboarding-dialog[open] {
      grid-template-columns: 1fr;
    }
    .welcome-sidebar {
      padding: 16px 20px;
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .welcome-sidebar ol,
    .welcome-sidebar p {
      display: none;
    }
    .welcome-sidebar h2 {
      font-size: 16px;
      margin: 0;
    }
    .guild-emblem {
      width: 36px;
      height: 36px;
      font-size: 14px;
      margin: 0;
      border-radius: 12px;
    }
    .welcome-main {
      max-height: calc(100dvh - 100px);
    }
    .welcome-content {
      padding: 20px;
    }
    .welcome-main header {
      padding: 12px 20px 0;
    }
    footer {
      padding: 16px 20px;
    }
    .answer-grid {
      grid-template-columns: 1fr;
    }
    .answer-card {
      min-height: 100px;
    }
    .section-heading,
    .save-row {
      align-items: stretch;
      flex-direction: column;
    }
    .onboarding-settings {
      padding: 16px;
    }
    .option-fields {
      grid-template-columns: 1fr;
    }
  }
</style>
