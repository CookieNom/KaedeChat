<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { operatorLegalConfig } from '$lib/branding/landing';
  import LegalPage from '$lib/components/landing/LegalPage.svelte';
  import UnconfiguredLegalPage from '$lib/components/landing/UnconfiguredLegalPage.svelte';
</script>

{#if operatorLegalConfig}
  <LegalPage title={$t('ui_privacy_policy_506ff394')} updated={operatorLegalConfig.effectiveDate}>
    <section>
      <h2>{$t('ui_1_short_version_59c42402')}</h2>
      <p>
        {$t('ui_we_run_value0_the_instance_a_chat_service_bui_c6069f2a', {
          value0: String(operatorLegalConfig.instanceName)
        })}
      </p>
    </section>

    <section>
      <h2>{$t('ui_2_who_we_are_41e5ab79')}</h2>
      <p>
        {$t('ui_value0_we_us_or_our_operates_the_instance_you_5c8efb8c', {
          value0: String(operatorLegalConfig.operatorName)
        })}
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >{$t('ui_this_policy_applies_to_this_instance_not_ever_25d81584')}
      </p>
    </section>

    <section>
      <h2>{$t('ui_3_what_we_collect_02390849')}</h2>
      <ul>
        <li>
          <strong>{$t('ui_account_details_36f70a76')}</strong>
          {$t('ui_your_username_or_handle_your_email_address_if_bae43ef4')}
        </li>
        <li>
          <strong>{$t('ui_content_you_share_759ea677')}</strong>
          {$t('ui_messages_media_files_and_reactions_you_post_s_e30c2e1c')}
        </li>
        <li>
          <strong>{$t('ui_service_and_operational_data_4216e4e4')}</strong>
          {$t('ui_guild_and_conversation_membership_contacts_pe_7874f0c0')}
        </li>
        <li>
          <strong>{$t('ui_support_requests_27146d5e')}</strong>
          {$t('ui_if_you_use_the_support_form_we_receive_the_em_6cba3148')}
        </li>
      </ul>
    </section>

    <section>
      <h2>{$t('ui_4_how_we_use_it_7bd15b00')}</h2>
      <ul>
        <li>{$t('ui_to_provide_the_chat_service_and_keep_it_worki_8f8054ab')}</li>
        <li>{$t('ui_to_secure_the_instance_and_investigate_abuse__def65a9e')}</li>
        <li>{$t('ui_to_detect_malware_and_known_child_sexual_abus_673d772f')}</li>
        <li>
          {$t('ui_to_enforce_the_a3de79a8')}
          <a href={resolve('terms')}>{$t('ui_terms_of_service_529f0e5f')}</a>
          {$t('ui_and_community_rules_b954c20b')}
        </li>
        <li>{$t('ui_to_respond_to_support_and_moderation_requests_69d9c052')}</li>
      </ul>
    </section>

    <section>
      <h2>{$t('ui_5_federation_bots_and_integrations_4c4cd404')}</h2>
      <p>{$t('ui_kaede_federates_when_you_interact_with_member_3a5fe556')}</p>
      <p>{$t('ui_bots_apps_and_bridges_that_you_or_a_community_2ad63282')}</p>
    </section>

    <section>
      <h2>{$t('ui_6_safety_checks_reports_and_end_to_end_encryp_cade2c83')}</h2>
      <p>{$t('ui_files_uploaded_outside_end_to_end_encrypted_r_078146b4')}</p>
      <p>{$t('ui_in_an_end_to_end_encrypted_room_your_device_e_d093d607')}</p>
      <p>{$t('ui_anyone_who_can_see_a_message_can_report_it_re_46b3d8ed')}</p>
      <p>{$t('ui_reports_usually_go_to_the_staff_of_the_instan_dbbba7fa')}</p>
      <p>{$t('ui_if_staff_confirm_child_sexual_abuse_material__7db27d27')}</p>
      <p>{$t('ui_for_each_case_we_keep_an_encrypted_record_of__f1e9680a')}</p>
    </section>

    <section>
      <h2>{$t('ui_7_service_providers_and_external_requests_ca9f51e9')}</h2>
      <p>{$t('ui_the_instance_may_use_separate_providers_for_h_86d840d6')}</p>
    </section>

    <section>
      <h2>{$t('ui_8_retention_ad366add')}</h2>
      <p>{$t('ui_account_records_and_content_normally_remain_u_956f90f7')}</p>
    </section>

    <section>
      <h2>{$t('ui_9_your_choices_e658c970')}</h2>
      <ul>
        <li>{$t('ui_you_may_update_supported_profile_and_account__08169800')}</li>
        <li>{$t('ui_in_settings_you_can_delete_all_your_content_o_dc761658')}</li>
        <li>{$t('ui_you_may_export_a_protected_recovery_bundle_fo_56584c58')}</li>
        <li>
          {$t('ui_for_other_privacy_requests_or_to_ask_us_to_di_9aa3d673')}
          <a href={`mailto:${operatorLegalConfig.contactEmail}`}
            >{operatorLegalConfig.contactEmail}</a
          >.
        </li>
        <li>{$t('ui_you_may_turn_off_available_notification_and_p_eb9d4b3e')}</li>
      </ul>
    </section>

    <section>
      <h2>{$t('ui_10_security_67a39824')}</h2>
      <p>{$t('ui_kaede_supports_encrypted_network_connections__057a2d87')}</p>
    </section>

    <section>
      <h2>{$t('ui_11_device_storage_and_external_content_af604b6b')}</h2>
      <p>{$t('ui_the_web_app_uses_http_only_authentication_coo_dc754f17')}</p>
    </section>

    <section>
      <h2>{$t('ui_12_children_144dca55')}</h2>
      <p>
        {$t('ui_the_instance_is_not_directed_at_children_unde_2f062f32', {
          value0: String(operatorLegalConfig.minimumAge)
        })}
      </p>
    </section>

    <section>
      <h2>{$t('ui_13_changes_to_this_policy_19939ee3')}</h2>
      <p>{$t('ui_we_may_update_this_policy_as_the_service_or_i_96756c85')}</p>
    </section>

    <section>
      <h2>{$t('ui_14_contact_d0a4f857')}</h2>
      <p>
        {$t('ui_for_privacy_questions_requests_or_to_exercise_bbcedb05')}
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >{$t('ui_what_rights_apply_depends_on_your_location_an_e7f3589a')}
      </p>
    </section>
  </LegalPage>
{:else}
  <UnconfiguredLegalPage title={$t('ui_privacy_policy_506ff394')} policy="privacy policy" />
{/if}
