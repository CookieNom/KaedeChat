<script lang="ts">
  import { t } from '$lib/ui/locale';

  import { resolve } from '$app/paths';
  import { operatorLegalConfig } from '$lib/branding/landing';
  import LegalPage from '$lib/components/landing/LegalPage.svelte';
  import UnconfiguredLegalPage from '$lib/components/landing/UnconfiguredLegalPage.svelte';
</script>

{#if operatorLegalConfig}
  <LegalPage title={$t('ui_terms_of_service_4afa55bf')} updated={operatorLegalConfig.effectiveDate}>
    <section>
      <h2>{$t('ui_1_about_these_terms_9c41a965')}</h2>
      <p>
        {$t('ui_these_terms_of_service_the_terms_apply_when_y_2be8ec43', {
          value0: String(operatorLegalConfig.instanceName),
          value1: String(operatorLegalConfig.operatorName)
        })}
      </p>
      <p>{$t('ui_kaede_is_the_software_that_powers_the_instanc_66d7230d')}</p>
    </section>

    <section>
      <h2>{$t('ui_2_accounts_389c59f8')}</h2>
      <ul>
        <li>{$t('ui_you_are_responsible_for_the_accuracy_of_the_i_4c9fdda9')}</li>
        <li>
          {$t('ui_you_must_be_at_least_value0_years_old_to_crea_b6afa5a6', {
            value0: String(operatorLegalConfig.minimumAge)
          })}
        </li>
        <li>{$t('ui_keep_your_credentials_private_we_cannot_verif_5e5a2e99')}</li>
        <li>{$t('ui_this_instance_secures_your_account_and_provid_3eb7d6e0')}</li>
      </ul>
    </section>

    <section>
      <h2>{$t('ui_3_acceptable_use_29858130')}</h2>
      <p>{$t('ui_when_you_use_the_instance_you_agree_not_to_53238b27')}</p>
      <ul>
        <li>{$t('ui_break_the_law_or_ask_someone_else_to_break_th_197a0fe1')}</li>
        <li>{$t('ui_harass_threaten_or_abuse_other_members_see_ou_b778ae14')}</li>
        <li>{$t('ui_upload_or_share_malware_phishing_or_other_har_83fb116b')}</li>
        <li>{$t('ui_share_someone_else_s_private_information_with_bfb936b2')}</li>
        <li>{$t('ui_send_unsolicited_bulk_messages_or_spam_010459bb')}</li>
        <li>{$t('ui_interfere_with_the_instance_including_attempt_7b311c7c')}</li>
        <li>{$t('ui_impersonate_another_person_or_misrepresent_yo_d64cd67a')}</li>
      </ul>
    </section>

    <section>
      <h2>{$t('ui_4_your_content_9be94705')}</h2>
      <p>{$t('ui_you_keep_the_rights_to_what_you_post_by_posti_f3006d58')}</p>
      <p>{$t('ui_you_are_responsible_for_your_own_content_we_m_bb2c284c')}</p>
    </section>

    <section>
      <h2>{$t('ui_5_storage_encryption_and_third_parties_fdff690a')}</h2>
      <p>
        {$t('ui_the_instance_stores_account_information_messa_40e093c9')}
        <a href={resolve('/privacy')}>{$t('ui_privacy_policy_2b72811d')}</a>
        {$t('ui_for_the_data_flows_and_choices_that_apply_3411e70d')}
      </p>
      <p>{$t('ui_if_you_use_end_to_end_encryption_keep_any_rec_372e2f9b')}</p>
    </section>

    <section>
      <h2>{$t('ui_6_moderation_640c3da6')}</h2>
      <p>{$t('ui_moderators_may_delete_content_remove_members__e186f148')}</p>
    </section>

    <section>
      <h2>{$t('ui_7_service_and_changes_70e16192')}</h2>
      <p>{$t('ui_the_instance_is_provided_as_is_and_as_availab_dbeb9e90')}</p>
    </section>

    <section>
      <h2>{$t('ui_8_termination_68c2c79d')}</h2>
      <p>
        {$t('ui_to_ask_us_to_disable_your_account_or_discuss__832a4f1e')}
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >{$t('ui_a_request_does_not_remove_copies_already_deli_fe16096b')}
      </p>
    </section>

    <section>
      <h2>{$t('ui_9_intellectual_property_b3bbeeea')}</h2>
      <p>{$t('ui_the_instance_s_branding_and_content_we_own_re_81e504f4')}</p>
    </section>

    <section>
      <h2>{$t('ui_10_disclaimers_and_limitation_of_liability_207a797b')}</h2>
      <p>{$t('ui_to_the_maximum_extent_permitted_by_law_we_dis_edce73f3')}</p>
    </section>

    <section>
      <h2>{$t('ui_11_governing_law_0c1f1919')}</h2>
      <p>
        {$t('ui_these_terms_are_governed_by_the_laws_of_value_1f39eaa6', {
          value0: String(operatorLegalConfig.jurisdiction),
          value1: String(operatorLegalConfig.jurisdiction)
        })}
      </p>
    </section>

    <section>
      <h2>{$t('ui_12_contact_5adadc20')}</h2>
      <p>
        {$t('ui_questions_about_these_terms_reach_us_at_3ea49024')}
        <a href={`mailto:${operatorLegalConfig.contactEmail}`}>{operatorLegalConfig.contactEmail}</a
        >.
      </p>
    </section>
  </LegalPage>
{:else}
  <UnconfiguredLegalPage title={$t('ui_terms_of_service_4afa55bf')} policy="terms of service" />
{/if}
