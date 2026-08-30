import { describe, expect, it } from 'vitest';
import pollResultVectors from '../../../static/protocol/poll-result-v1.json';
import type { Message } from './types';
import {
  componentInteractionBody,
  embedAccent,
  ephemeralComponentInteractionBody,
  entitySelectOptions,
  fileUploadAccept,
  fileUploadMatches,
  interactionResponseAttachments,
  interactionResponseHasMessageContent,
  interactionResponsePoll,
  messagePollResult,
  modalSubmitBody,
  modalFromInteractionEvent,
  pollAnswerPercent,
  pollIsClosed,
  pollTotalVotes,
  selectDefaultValues,
  selectSubmissionState,
  type EntitySelectComponent,
  type MessagePoll
} from './rich-content';

const poll: MessagePoll = {
  question: { text: 'Ship it?' },
  answers: [
    { answer_id: 1, poll_media: { text: 'Yes' } },
    { answer_id: 2, poll_media: { text: 'No' } }
  ],
  expiry: '2099-01-01T00:00:00Z',
  allow_multiselect: false,
  layout_type: 1,
  results: {
    is_finalized: false,
    answer_counts: [
      { id: 1, count: 3, me_voted: true },
      { id: 2, count: 1, me_voted: false }
    ]
  }
};

describe('rich message contracts', () => {
  it('projects and enforces Discord-style file picker filters', () => {
    expect(fileUploadAccept(['image', '.pdf'])).toBe('image/*,.pdf');
    expect(fileUploadMatches(['image', '.pdf'], 'photo.PNG', 'image/png')).toBe(true);
    expect(fileUploadMatches(['image', '.pdf'], 'report.PDF', 'application/pdf')).toBe(true);
    expect(fileUploadMatches(['image', '.pdf'], 'payload.exe', 'application/octet-stream')).toBe(
      false
    );
  });

  it('calculates stable poll totals and percentages', () => {
    expect(pollTotalVotes(poll)).toBe(4);
    expect(pollAnswerPercent(poll, 1)).toBe(75);
    expect(pollAnswerPercent({ ...poll, results: { ...poll.results, answer_counts: [] } }, 1)).toBe(
      0
    );
    expect(pollIsClosed(poll, Date.parse('2098-01-01T00:00:00Z'))).toBe(false);
  });

  it('consumes strict shared type-46 poll result vectors without leaking E2EE labels', () => {
    for (const vector of pollResultVectors.vectors) {
      const message = vector.message as unknown as Message;
      const verifiedSource =
        vector.name === 'e2ee_unique_winner_federated_ref'
          ? ({
              id: '456',
              origin_domain: 'author.example',
              e2ee: { version: 2 },
              e2ee_verified: true,
              poll: {
                ...vector.verified_source_poll,
                expiry: '2099-01-01T00:00:00Z',
                allow_multiselect: false,
                layout_type: 1,
                results: {
                  is_finalized: true,
                  answer_counts: [
                    { id: 1, count: 2, me_voted: false },
                    { id: 2, count: 5, me_voted: false }
                  ]
                }
              }
            } as unknown as Message)
          : null;
      const result = messagePollResult(message, verifiedSource);
      expect(result).not.toBeNull();
      if (vector.name === 'e2ee_unique_winner_federated_ref') {
        expect(result?.question_text).toBe('Secret launch choice');
        expect(result?.victor_answer_text).toBe('Launch');
      }
    }
  });

  it('fails closed for inconsistent or privacy-leaking type-46 projections', () => {
    const base = structuredClone(pollResultVectors.vectors[1].message) as unknown as Message;
    expect(messagePollResult({ ...base, referenced_message_id: '455' })).toBeNull();
    expect(
      messagePollResult({
        ...base,
        embeds: [
          {
            ...(base.embeds?.[0] ?? {}),
            fields: [
              ...(base.embeds?.[0]?.fields ?? []),
              { name: 'poll_question_text', value: 'secret', inline: false }
            ]
          }
        ]
      })
    ).toBeNull();
    expect(
      messagePollResult({
        ...base,
        poll_result: {
          ...(base.poll_result ?? {}),
          answer_counts: [
            { id: 1, count: 2 },
            { id: 2, count: 6 }
          ]
        }
      })
    ).toBeNull();
    expect(
      messagePollResult(base, {
        id: '456',
        origin_domain: 'author.example',
        e2ee: null
      } as unknown as Message)
    ).toBeNull();
  });

  it('builds a correlated component interaction only for application messages', () => {
    const message = {
      id: '1',
      origin_domain: 'chat.example',
      application_id: '2',
      application_domain: 'chat.example'
    } as Message;
    expect(
      componentInteractionBody(message, { type: 2, style: 1, label: 'Go', custom_id: 'go' })
    ).toEqual({
      application_ref: '2@chat.example',
      interaction_type: 'component',
      message_ref: '1@chat.example',
      custom_id: 'go',
      values: []
    });
    expect(
      componentInteractionBody(
        { ...message, application_id: null },
        { type: 2, style: 1, label: 'Go', custom_id: 'go' }
      )
    ).toBeNull();
  });

  it('targets an isolated ephemeral view by response and version', () => {
    expect(
      ephemeralComponentInteractionBody(
        '2@chat.example',
        '99',
        3,
        { type: 2, style: 1, custom_id: 'next' },
        ['yes']
      )
    ).toEqual({
      application_ref: '2@chat.example',
      interaction_type: 'component',
      response_id: '99',
      view_version: 3,
      custom_id: 'next',
      values: ['yes']
    });
  });

  it('filters channel selects and parses modal callback envelopes', () => {
    const component: EntitySelectComponent = { type: 8, custom_id: 'channel', channel_types: [0] };
    expect(
      entitySelectOptions(component, {
        users: [],
        roles: [],
        channels: [
          { id: '1', origin_domain: 'chat.example', name: 'general', type: 0 } as never,
          { id: '2', origin_domain: 'chat.example', name: 'voice', type: 2 } as never
        ]
      })
    ).toEqual([{ value: '1@chat.example', label: '#general', type: 'channel' }]);
    expect(
      modalFromInteractionEvent({
        interaction_id: '4',
        response_type: 9,
        data: { title: 'Details', custom_id: 'details', components: [] }
      })
    ).toEqual({ title: 'Details', custom_id: 'details', components: [] });
    expect(embedAccent(0x12ab)).toBe('#0012ab');
  });

  it('stages multi-select values until the authored min/max contract is satisfied', () => {
    const component = {
      type: 3,
      custom_id: 'labels',
      min_values: 2,
      max_values: 3,
      options: [
        { label: 'One', value: 'one', default: true },
        { label: 'Two', value: 'two' },
        { label: 'Three', value: 'three' }
      ]
    } satisfies import('./rich-content').StringSelectComponent;
    expect(selectDefaultValues(component)).toEqual(['one']);
    expect(selectSubmissionState(component, ['one'])).toMatchObject({ staged: true, valid: false });
    expect(selectSubmissionState(component, ['one', 'two'])).toMatchObject({
      staged: true,
      valid: true,
      minimum: 2,
      maximum: 3
    });
  });

  it('correlates modal submissions and preserves typed field values', () => {
    const message = {
      id: '1',
      origin_domain: 'chat.example',
      application_id: '2',
      application_domain: 'chat.example'
    } as Message;
    expect(
      modalSubmitBody(
        message,
        'response-9',
        {
          title: 'Details',
          custom_id: 'details',
          components: [
            {
              type: 10,
              content: 'Choose labels'
            },
            {
              type: 18,
              label: 'Labels',
              component: {
                type: 3,
                custom_id: 'labels',
                options: [{ label: 'Urgent', value: 'urgent' }]
              }
            },
            {
              type: 18,
              label: 'Notify me',
              component: { type: 23, custom_id: 'notify' }
            },
            {
              type: 18,
              label: 'Priority',
              component: {
                type: 21,
                custom_id: 'priority',
                required: false,
                options: [{ label: 'Normal', value: 'normal' }]
              }
            },
            {
              type: 18,
              label: 'Evidence',
              component: {
                type: 19,
                custom_id: 'evidence',
                required: false,
                min_values: 0,
                max_values: 2,
                file_types: ['image', '.pdf']
              }
            }
          ]
        },
        { labels: ['urgent'], notify: true, priority: null, evidence: ['91'] }
      )
    ).toEqual({
      application_ref: '2@chat.example',
      interaction_type: 'modal_submit',
      response_id: 'response-9',
      custom_id: 'details',
      components: [
        {
          type: 18,
          component: { type: 3, custom_id: 'labels', values: ['urgent'] }
        },
        {
          type: 18,
          component: { type: 23, custom_id: 'notify', value: true }
        },
        {
          type: 18,
          component: { type: 21, custom_id: 'priority', value: null }
        },
        {
          type: 18,
          component: {
            type: 19,
            custom_id: 'evidence',
            values: ['91']
          }
        }
      ]
    });
    expect(
      modalSubmitBody(message, '', { title: 'Details', custom_id: 'details', components: [] }, {})
    ).toBeNull();
  });

  it('safely projects private response attachments and poll drafts', () => {
    const data = {
      attachments: [
        {
          id: '90',
          origin_domain: 'chat.example',
          filename: '../\u0000release.png',
          content_type: 'image/png',
          size: 4096,
          width: 800,
          height: 600,
          blurhash: null,
          scan_status: 'clean',
          private_media_url:
            '/api/v1/interactions/70@chat.example/responses/71@chat.example/attachments/90@chat.example',
          variants: { thumbnail_512: { width: 512, height: 384 } }
        },
        {
          id: '../bad',
          origin_domain: 'attacker.invalid',
          filename: 'ignored.txt',
          content_type: 'text/plain',
          size: 1,
          scan_status: 'clean'
        }
      ],
      poll: {
        question: { text: 'Ship it?' },
        answers: [{ poll_media: { text: 'Yes' } }, { poll_media: { text: 'Wait' } }],
        duration: 24,
        allow_multiselect: false,
        layout_type: 1
      }
    };

    expect(interactionResponseAttachments(data)).toMatchObject([
      {
        id: '90',
        origin_domain: 'chat.example',
        filename: 'release.png',
        content_type: 'image/png',
        scan_status: 'clean',
        private_media_url:
          '/api/v1/interactions/70@chat.example/responses/71@chat.example/attachments/90@chat.example'
      }
    ]);
    expect(interactionResponsePoll(data)).toMatchObject({
      question: { text: 'Ship it?' },
      answers: [{ answer_id: 1 }, { answer_id: 2 }],
      results: {
        answer_counts: [
          { id: 1, count: 0, me_voted: false },
          { id: 2, count: 0, me_voted: false }
        ]
      }
    });
    expect(interactionResponseHasMessageContent(data)).toBe(true);
  });
});
