<script lang="ts">
  import type { Attachment } from '$lib/chat/types';
  import VoiceMessageFrame from './VoiceMessageFrame.svelte';
  import { attachmentMediaPath, authenticatedMedia } from '$lib/media/authenticated';

  let { attachment, onError }: { attachment: Attachment; onError?: (event: Event) => void } =
    $props();
</script>

<VoiceMessageFrame duration={attachment.duration_secs} waveform={attachment.waveform}>
  <audio
    use:authenticatedMedia={{
      path: attachmentMediaPath(
        attachment.origin_domain,
        attachment.id,
        'original',
        attachment.history_media_url
      ),
      contentType: attachment.content_type
    }}
    onerror={onError}
    controls
    preload="metadata"
    aria-label={`Play voice message ${attachment.filename}`}
  >
    <track kind="captions" />
  </audio>
</VoiceMessageFrame>
