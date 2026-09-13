<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { api, userErrorMessage } from '$lib/api/client';
  import type { BotInviteReference } from '$lib/chat/bot-invites';
  import { assetUrl } from '$lib/media/assets';

  let { reference }: { reference: BotInviteReference } = $props();
  interface Invite {
    application: {
      id: string;
      origin_domain: string;
      name: string;
      description: string | null;
      bot_user: {
        id: string;
        origin_domain: string;
        username: string;
        display_name: string | null;
        avatar_hash: string | null;
      };
    };
    template: {
      name: string;
      description: string | null;
      scopes: string[];
      e2ee_mode: string;
    };
  }
  let invite = $state<Invite | null>(null);
  let unavailable = $state('');
  let generation = 0;

  $effect(() => {
    const target = reference;
    const current = ++generation;
    invite = null;
    unavailable = '';
    void api<Invite>(
      `/bot-invites/${encodeURIComponent(target.applicationRef)}/${encodeURIComponent(target.templateSlug)}`
    )
      .then((value) => {
        if (current === generation) invite = value;
      })
      .catch((caught) => {
        if (current === generation)
          unavailable = userErrorMessage(
            caught,
            $t('ui_this_bot_invitation_is_unavailable_b2641d14')
          );
      });
  });
</script>

{#if invite}
  <aside class="bot-invite" aria-label={`Bot invitation for ${invite.application.name}`}>
    <div class="identity">
      <span class="avatar">
        {#if invite.application.bot_user.avatar_hash}
          <img
            src={assetUrl(
              invite.application.bot_user.avatar_hash,
              'thumbnail_128',
              invite.application.bot_user
            )}
            alt=""
          />
        {:else}{invite.application.name.slice(0, 1).toUpperCase()}{/if}
      </span>
      <span
        ><small>{$t('ui_app_authorization_08b9aae0')}</small><strong
          >{invite.application.name}</strong
        ><em>{invite.application.origin_domain}</em></span
      >
    </div>
    <p>{invite.template.description ?? invite.application.description ?? invite.template.name}</p>
    <div class="access">
      <span
        >{$t('ui_value0_api_scopes_53034ee3', {
          value0: String(invite.template.scopes.length)
        })}</span
      >
      <span
        >{$t('ui_value0_e2ee_042e0b59', {
          value0: String(invite.template.e2ee_mode.replaceAll('_', ' '))
        })}</span
      >
    </div>
    <a
      href={resolve('/(app)/applications/[applicationRef]/install/[templateSlug]', {
        applicationRef: reference.applicationRef,
        templateSlug: reference.templateSlug
      })}>{$t('ui_review_and_add_9f92d110')}</a
    >
  </aside>
{:else if unavailable}
  <aside class="bot-invite unavailable">
    <strong>{$t('ui_bot_invitation_unavailable_04aa9c1e')}</strong><small>{unavailable}</small>
  </aside>
{:else}
  <aside class="bot-invite loading" aria-label={$t('ui_loading_bot_invitation_f19e849a')}>
    <span></span>
  </aside>
{/if}

<style>
  .bot-invite {
    display: grid;
    width: min(430px, 100%);
    box-sizing: border-box;
    gap: 11px;
    margin-top: 10px;
    padding: 16px;
    border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    border-radius: 14px;
    background: color-mix(in srgb, var(--surface-raised) 92%, var(--accent));
  }
  .identity {
    display: flex;
    gap: 11px;
    align-items: center;
  }
  .identity > span:last-child {
    display: grid;
    min-width: 0;
  }
  .identity small {
    color: var(--accent);
    font-size: 0.69rem;
    font-weight: 800;
    letter-spacing: 0.08em;
  }
  .identity strong,
  .identity em {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .identity em {
    color: var(--muted);
    font-size: 0.82rem;
    font-style: normal;
  }
  .avatar {
    display: grid;
    width: 48px;
    height: 48px;
    flex: 0 0 48px;
    overflow: hidden;
    place-items: center;
    border-radius: 15px;
    color: white;
    background: var(--accent-deep);
    font-weight: 800;
  }
  .avatar img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  p {
    margin: 0;
    color: var(--text-muted);
  }
  .access {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
  }
  .access span {
    padding: 4px 8px;
    border-radius: 999px;
    background: var(--surface);
    color: var(--muted);
    font-size: 0.75rem;
  }
  a {
    justify-self: start;
    padding: 8px 12px;
    border-radius: 9px;
    color: white;
    background: var(--accent);
    font-weight: 750;
    text-decoration: none;
  }
  .unavailable small {
    color: var(--muted);
  }
  .loading {
    min-height: 80px;
  }
  .loading span {
    width: 55%;
    height: 15px;
    border-radius: 6px;
    background: var(--surface);
  }
</style>
