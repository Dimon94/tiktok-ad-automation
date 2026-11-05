// 由 scripts/generate-demo-operations.mjs 生成，请勿手改。
import type {
  accountsAuthorizeData,
  accountsAuthorizeResponse,
  accountsBindingData,
  accountsBindingResponse,
  accountsCandidateBcsData,
  accountsCandidateBcsResponse,
  accountsCapabilityStatusData,
  accountsCapabilityStatusResponse,
  accountsConfigurationData,
  accountsConfigurationResponse,
  accountsDisableData,
  accountsDisableResponse,
  accountsGetAccountsData,
  accountsGetAccountsResponse,
  accountsGetBcsData,
  accountsGetBcsResponse,
  accountsGetConfigurationData,
  accountsGetConfigurationResponse,
  accountsGetConnectionsData,
  accountsGetConnectionsResponse,
  accountsPatchConnectionData,
  accountsPatchConnectionResponse,
  accountsPostAuthorizationData,
  accountsPostAuthorizationResponse,
  accountsPostResolveData,
  accountsPostResolveResponse,
  accountsPutDefaultConnectionData,
  accountsPutDefaultConnectionResponse,
  accountsRefreshCapabilitiesData,
  accountsRefreshCapabilitiesResponse,
  accountsRevokeData,
  accountsRevokeResponse,
  acknowledgeIngestPartsData,
  acknowledgeIngestPartsResponse,
  adManagementCancelTaskData,
  adManagementCancelTaskResponse,
  adManagementGetTaskData,
  adManagementGetTaskResponse,
  adManagementListTasksData,
  adManagementListTasksResponse,
  adManagementPreparePreviewData,
  adManagementPreparePreviewResponse,
  adManagementPrepareRestoreData,
  adManagementPrepareRestoreResponse,
  adManagementReconcileTaskData,
  adManagementReconcileTaskResponse,
  adManagementResolveRenameSelectionData,
  adManagementResolveRenameSelectionResponse,
  adManagementRetryTargetedRefreshData,
  adManagementRetryTargetedRefreshResponse,
  adManagementRetryTaskData,
  adManagementRetryTaskResponse,
  adManagementSubmitDirectTaskData,
  adManagementSubmitDirectTaskResponse,
  adManagementSubmitTaskData,
  adManagementSubmitTaskResponse,
  addBindingsData,
  addBindingsResponse,
  adsReportingAdDetailData,
  adsReportingAdDetailResponse,
  adsReportingAdMaterialsData,
  adsReportingAdMaterialsResponse,
  adsReportingAdTrendData,
  adsReportingAdTrendResponse,
  adsReportingCreateReportExportData,
  adsReportingCreateReportExportResponse,
  adsReportingCreateReportViewData,
  adsReportingCreateReportViewResponse,
  adsReportingDeleteReportViewData,
  adsReportingDeleteReportViewResponse,
  adsReportingDownloadReportExportData,
  adsReportingFreezeAdMaterialSelectionData,
  adsReportingFreezeAdMaterialSelectionResponse,
  adsReportingFreezeAdSelectionData,
  adsReportingFreezeAdSelectionResponse,
  adsReportingGetAdSyncStatusData,
  adsReportingGetAdSyncStatusResponse,
  adsReportingGetReportExportData,
  adsReportingGetReportExportResponse,
  adsReportingListReportExportsData,
  adsReportingListReportExportsResponse,
  adsReportingListReportViewsData,
  adsReportingListReportViewsResponse,
  adsReportingMetricCatalogData,
  adsReportingMetricCatalogResponse,
  adsReportingQueryAdsData,
  adsReportingQueryAdsResponse,
  adsReportingRequestAdSyncRefreshData,
  adsReportingRequestAdSyncRefreshResponse,
  adsReportingUpdateReportViewData,
  adsReportingUpdateReportViewResponse,
  automationRulesBatchRulesData,
  automationRulesBatchRulesResponse,
  automationRulesConfirmBatchData,
  automationRulesConfirmBatchResponse,
  automationRulesCreateRuleData,
  automationRulesCreateRuleResponse,
  automationRulesDeleteRuleData,
  automationRulesDeleteRuleResponse,
  automationRulesDismissBatchData,
  automationRulesDismissBatchResponse,
  automationRulesDryRunData,
  automationRulesDryRunResponse,
  automationRulesGetCatalogData,
  automationRulesGetCatalogResponse,
  automationRulesGetConfirmationData,
  automationRulesGetConfirmationResponse,
  automationRulesGetOverviewData,
  automationRulesGetOverviewResponse,
  automationRulesGetRuleData,
  automationRulesGetRuleResponse,
  automationRulesGetRunData,
  automationRulesGetRunResponse,
  automationRulesGetSettingsData,
  automationRulesGetSettingsResponse,
  automationRulesListConfirmationsData,
  automationRulesListConfirmationsResponse,
  automationRulesListRulesData,
  automationRulesListRulesResponse,
  automationRulesListRunsData,
  automationRulesListRunsResponse,
  automationRulesListTemplatesData,
  automationRulesListTemplatesResponse,
  automationRulesPickAccountsData,
  automationRulesPickAccountsResponse,
  automationRulesPickObjectsData,
  automationRulesPickObjectsResponse,
  automationRulesPutSettingsData,
  automationRulesPutSettingsResponse,
  automationRulesSetEnabledData,
  automationRulesSetEnabledResponse,
  automationRulesTestFeishuData,
  automationRulesTestFeishuResponse,
  automationRulesUpdateRuleData,
  automationRulesUpdateRuleResponse,
  availableBcsData,
  availableBcsResponse,
  buildsAddCandidatesData,
  buildsAddCandidatesResponse,
  buildsAdoptData,
  buildsAdoptResponse,
  buildsAllocateData,
  buildsAllocateResponse,
  buildsApplyData,
  buildsApplyResponse,
  buildsAuthorizeHistoricalReadData,
  buildsAuthorizeHistoricalReadResponse,
  buildsCancelCandidatesData,
  buildsCancelCandidatesResponse,
  buildsCancelData,
  buildsCancelResponse,
  buildsCandidateProgressData,
  buildsCandidateProgressResponse,
  buildsCandidatesData,
  buildsCandidatesResponse,
  buildsCompositionStateData,
  buildsCompositionStateResponse,
  buildsCreateData,
  buildsCreateResponse,
  buildsDifferencesData,
  buildsDifferencesResponse,
  buildsDramasData,
  buildsDramasResponse,
  buildsEditData,
  buildsEditGroupsData,
  buildsEditGroupsResponse,
  buildsEditResponse,
  buildsExcludeData,
  buildsExcludeResponse,
  buildsFrozenAdsData,
  buildsFrozenAdsResponse,
  buildsFrozenGroupsData,
  buildsFrozenGroupsResponse,
  buildsFrozenUnitData,
  buildsFrozenUnitResponse,
  buildsGeneratePreviewData,
  buildsGeneratePreviewResponse,
  buildsGetHistoricalReadData,
  buildsGetHistoricalReadRequestData,
  buildsGetHistoricalReadRequestResponse,
  buildsGetHistoricalReadResponse,
  buildsGetSubmissionAdsData,
  buildsGetSubmissionAdsResponse,
  buildsGetSubmissionData,
  buildsGetSubmissionEventsData,
  buildsGetSubmissionEventsResponse,
  buildsGetSubmissionExcludedData,
  buildsGetSubmissionExcludedResponse,
  buildsGetSubmissionGroupsData,
  buildsGetSubmissionGroupsResponse,
  buildsGetSubmissionMaterialsData,
  buildsGetSubmissionMaterialsResponse,
  buildsGetSubmissionRecoveryData,
  buildsGetSubmissionRecoveryResponse,
  buildsGetSubmissionResponse,
  buildsGetSubmissionStepsData,
  buildsGetSubmissionStepsResponse,
  buildsGetSubmissionUnitsData,
  buildsGetSubmissionUnitsResponse,
  buildsIdentityOptionsData,
  buildsIdentityOptionsResponse,
  buildsImageDramasData,
  buildsImageDramasResponse,
  buildsInputsData,
  buildsInputsResponse,
  buildsListDraftsData,
  buildsListDraftsResponse,
  buildsListSubmissionsData,
  buildsListSubmissionsResponse,
  buildsMaterialsData,
  buildsMaterialsResponse,
  buildsMinisOptionsData,
  buildsMinisOptionsResponse,
  buildsMusicProgressData,
  buildsMusicProgressResponse,
  buildsMusicRetryBatchesData,
  buildsMusicRetryBatchesResponse,
  buildsMusicRetryProgressData,
  buildsMusicRetryProgressResponse,
  buildsMusicSummaryData,
  buildsMusicSummaryResponse,
  buildsPlanData,
  buildsPlanResponse,
  buildsPlanUnitsData,
  buildsPlanUnitsResponse,
  buildsPrepareData,
  buildsPrepareResponse,
  buildsPreviewData,
  buildsPreviewDramasData,
  buildsPreviewDramasResponse,
  buildsPreviewInputsData,
  buildsPreviewInputsResponse,
  buildsPreviewRequestData,
  buildsPreviewRequestResponse,
  buildsPreviewResponse,
  buildsPreviewSummaryData,
  buildsPreviewSummaryResponse,
  buildsPreviewUnitsData,
  buildsPreviewUnitsResponse,
  buildsProgressData,
  buildsProgressResponse,
  buildsPutManualLinkData,
  buildsPutManualLinkResponse,
  buildsReadMusicData,
  buildsReadMusicResponse,
  buildsReconcileSubmissionData,
  buildsReconcileSubmissionResponse,
  buildsRefreshData,
  buildsRefreshIdentitiesCatalogData,
  buildsRefreshIdentitiesCatalogResponse,
  buildsRefreshMinisCatalogData,
  buildsRefreshMinisCatalogResponse,
  buildsRefreshResponse,
  buildsRequestMusicData,
  buildsRequestMusicResponse,
  buildsRetrySelectedMusicData,
  buildsRetrySelectedMusicResponse,
  buildsRetrySubmissionData,
  buildsRetrySubmissionResponse,
  buildsSavedMutationData,
  buildsSavedMutationResponse,
  buildsSavedPrepareRequestData,
  buildsSavedPrepareRequestResponse,
  buildsSavedRequestData,
  buildsSavedRequestResponse,
  buildsSavedSubmissionData,
  buildsSavedSubmissionRecoveryData,
  buildsSavedSubmissionRecoveryResponse,
  buildsSavedSubmissionResponse,
  buildsSearchMusicData,
  buildsSearchMusicResponse,
  buildsSelectIdentityData,
  buildsSelectIdentityResponse,
  buildsSelectMiniData,
  buildsSelectMiniResponse,
  buildsSkippedMaterialsData,
  buildsSkippedMaterialsResponse,
  buildsSubmitPreviewData,
  buildsSubmitPreviewResponse,
  buildsSummaryData,
  buildsSummaryResponse,
  buildsTargetingAccountsData,
  buildsTargetingAccountsResponse,
  buildsTargetingOptionsData,
  buildsTargetingOptionsResponse,
  buildsTargetingRegionsData,
  buildsTargetingRegionsResponse,
  buildsUnitsData,
  buildsUnitsResponse,
  buildsUpdateData,
  buildsUpdateDeliveryData,
  buildsUpdateDeliveryResponse,
  buildsUpdateImageStructureData,
  buildsUpdateImageStructureResponse,
  buildsUpdateResponse,
  buildsUpdateTargetingData,
  buildsUpdateTargetingResponse,
  businessDashboardData,
  businessDashboardResponse,
  businessDownloadData,
  businessDramasData,
  businessDramasResponse,
  businessEvidenceData,
  businessEvidenceResponse,
  businessExportData,
  businessExportResponse,
  businessExportStatusData,
  businessExportStatusResponse,
  businessMappingData,
  businessMappingResponse,
  businessMemberData,
  businessMemberResponse,
  businessOptionsData,
  businessOptionsResponse,
  businessRatesData,
  businessRatesResponse,
  businessReadEvidenceData,
  businessReadEvidenceResponse,
  businessRebuildData,
  businessRebuildResponse,
  businessRunsData,
  businessRunsResponse,
  businessSetRateData,
  businessSetRateResponse,
  cancelIngestFileData,
  cancelIngestFileResponse,
  completeIngestFileData,
  completeIngestFileResponse,
  createIngestChunkData,
  createIngestChunkResponse,
  createIngestGenerationData,
  createIngestGenerationResponse,
  createIngestSessionData,
  createIngestSessionResponse,
  integrationsCallbackData,
  integrationsTiktokCallbackData,
  listIngestFilesData,
  listIngestFilesResponse,
  listIngestPartsData,
  listIngestPartsResponse,
  listIngestSessionsData,
  listIngestSessionsResponse,
  loginLoginAccessTokenData,
  loginLoginAccessTokenResponse,
  loginTestTokenData,
  loginTestTokenResponse,
  materialProductionGetMaterialProductionData,
  materialProductionGetMaterialProductionResponse,
  materialProductionPostMaterialLoginLinkData,
  materialProductionPostMaterialLoginLinkResponse,
  materialProductionRetryMaterialProductionData,
  materialProductionRetryMaterialProductionResponse,
  materialPushGetExternalSourceData,
  materialPushGetMaterialBatchData,
  materialPushGetMaterialBatchResponse,
  materialPushPostMaterialBatchData,
  materialPushPostMaterialBatchResponse,
  materialsAuthorizeReissueData,
  materialsAuthorizeReissueResponse,
  materialsCopyVersionData,
  materialsCopyVersionResponse,
  materialsGetAssetsData,
  materialsGetAssetsResponse,
  materialsGetAttemptsData,
  materialsGetAttemptsResponse,
  materialsGetMaterialData,
  materialsGetMaterialResponse,
  materialsGetMaterialsData,
  materialsGetMaterialsResponse,
  materialsListVersionsData,
  materialsListVersionsResponse,
  materialsLoadVersionData,
  materialsLoadVersionResponse,
  materialsPostCompleteData,
  materialsPostCompleteResponse,
  materialsPostObjectRetryData,
  materialsPostObjectRetryResponse,
  materialsPostPartSignatureData,
  materialsPostPartSignatureResponse,
  materialsPostUploadBatchData,
  materialsPostUploadBatchResponse,
  materialsReadAccountVideoPreviewData,
  materialsReadAccountVideoPreviewResponse,
  materialsReadOriginalPreviewData,
  materialsReadOriginalPreviewResponse,
  materialsReadRemotePreviewData,
  materialsReadRemotePreviewResponse,
  materialsReadUploadBatchData,
  materialsReadUploadBatchResponse,
  materialsReadUploadBatchesData,
  materialsReadUploadBatchesResponse,
  materialsReadUploadRequestData,
  materialsReadUploadRequestResponse,
  materialsReadVersionData,
  materialsReadVersionResponse,
  materialsSaveVersionData,
  materialsSaveVersionResponse,
  providersApplicationCapabilitiesData,
  providersApplicationCapabilitiesResponse,
  providersGetLinksData,
  providersGetLinksResponse,
  providersGetPreparationData,
  providersGetPreparationResponse,
  providersGetPreparationSummaryData,
  providersGetPreparationSummaryResponse,
  providersLinkDetailsData,
  providersLinkDetailsResponse,
  providersListApplicationsData,
  providersListApplicationsResponse,
  providersListConnectionsData,
  providersListConnectionsResponse,
  providersPatchApplicationLinkDefaultsData,
  providersPatchApplicationLinkDefaultsResponse,
  providersPatchConnectionData,
  providersPatchConnectionResponse,
  providersPostCandidateData,
  providersPostCandidateResponse,
  providersPostConnectionData,
  providersPostConnectionResponse,
  providersPostPreparationData,
  providersPostPreparationResponse,
  providersPostVerifyData,
  providersPostVerifyResponse,
  readIngestChunkData,
  readIngestChunkResponse,
  readIngestFileData,
  readIngestFileResponse,
  readIngestPartPermissionsData,
  readIngestPartPermissionsResponse,
  readIngestRequestData,
  readIngestRequestResponse,
  readIngestSummaryData,
  readIngestSummaryResponse,
  resumeIngestFileData,
  resumeIngestFileResponse,
  sealIngestSessionData,
  sealIngestSessionResponse,
  signIngestPartsData,
  signIngestPartsResponse,
  strategiesAppendData,
  strategiesAppendResponse,
  strategiesCopyPoolData,
  strategiesCopyPoolResponse,
  strategiesCreateData,
  strategiesCreateResponse,
  strategiesGetOneData,
  strategiesGetOneResponse,
  strategiesGetStrategiesData,
  strategiesGetStrategiesResponse,
  strategiesSavedRequestData,
  strategiesSavedRequestResponse,
  strategiesSetStateData,
  strategiesSetStateResponse,
  strategiesStructureEstimateData,
  strategiesStructureEstimateResponse,
  strategiesValidateData,
  strategiesValidateResponse,
  strategiesVersionData,
  strategiesVersionResponse,
  strategiesVersionsData,
  strategiesVersionsResponse,
  syncApiConnectionData,
  syncApiConnectionResponse,
  syncMcpBcData,
  syncMcpBcResponse,
  tenantsGetMemberCandidatesData,
  tenantsGetMemberCandidatesResponse,
  tenantsGetMembersData,
  tenantsGetMembersResponse,
  tenantsGetMyTenantsData,
  tenantsGetMyTenantsResponse,
  tenantsGetPlatformTenantsData,
  tenantsGetPlatformTenantsResponse,
  tenantsGetPlatformUserCandidatesData,
  tenantsGetPlatformUserCandidatesResponse,
  tenantsPatchTenantData,
  tenantsPatchTenantResponse,
  tenantsPostMemberUserData,
  tenantsPostMemberUserResponse,
  tenantsPostTenantData,
  tenantsPostTenantResponse,
  tenantsPutMemberData,
  tenantsPutMemberResponse,
  unbindMcpBcData,
  unbindMcpBcResponse,
  usersCreateUserData,
  usersCreateUserResponse,
  usersDeleteUserData,
  usersDeleteUserMeData,
  usersDeleteUserMeResponse,
  usersDeleteUserResponse,
  usersReadUserByIdData,
  usersReadUserByIdResponse,
  usersReadUserMeData,
  usersReadUserMeResponse,
  usersReadUsersData,
  usersReadUsersResponse,
  usersUpdatePasswordMeData,
  usersUpdatePasswordMeResponse,
  usersUpdateUserData,
  usersUpdateUserMeData,
  usersUpdateUserMeResponse,
  usersUpdateUserResponse,
  utilsHealthCheckData,
  utilsHealthCheckResponse,
} from "@/client/types.gen"

export const DEMO_OPERATIONS = {
  "accounts-authorize": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/mcp/authorizations", query: {} },
  "accounts-binding": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/mcp/candidates/{attempt_id}/binding", query: {} },
  "accounts-candidate_bcs": { method: "GET", path: "/api/tenants/{tenant_id}/tiktok/mcp/candidates/{attempt_id}/bcs", query: {"page":"number","page_size":"number"} },
  "accounts-capability_status": { method: "GET", path: "/api/tenants/{tenant_id}/bcs/{bc_id}/capability-refresh/{job_id}", query: {} },
  "accounts-configuration": { method: "GET", path: "/api/tenants/{tenant_id}/tiktok/mcp/configuration", query: {} },
  "accounts-disable": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/mcp/connections/{connection_id}/disable", query: {} },
  "accounts-get_accounts": { method: "GET", path: "/api/tenants/{tenant_id}/accounts", query: {"limit":"number"} },
  "accounts-get_bcs": { method: "GET", path: "/api/tenants/{tenant_id}/bcs", query: {"limit":"number"} },
  "accounts-get_configuration": { method: "GET", path: "/api/tenants/{tenant_id}/tiktok/configuration", query: {} },
  "accounts-get_connections": { method: "GET", path: "/api/tenants/{tenant_id}/tiktok/connections", query: {"limit":"number"} },
  "accounts-patch_connection": { method: "PATCH", path: "/api/tenants/{tenant_id}/tiktok/connections/{connection_id}", query: {} },
  "accounts-post_authorization": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/authorizations", query: {} },
  "accounts-post_resolve": { method: "POST", path: "/api/tenants/{tenant_id}/accounts/resolve", query: {} },
  "accounts-put_default_connection": { method: "PUT", path: "/api/tenants/{tenant_id}/bcs/{bc_id}/default-connection", query: {} },
  "accounts-refresh_capabilities": { method: "POST", path: "/api/tenants/{tenant_id}/bcs/{bc_id}/capability-refresh", query: {} },
  "accounts-revoke": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/mcp/connections/{connection_id}/revocations", query: {} },
  "acknowledge_ingest_parts": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/part-receipts", query: {} },
  "ad_management-cancel_task": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-tasks/{task_id}/cancel", query: {} },
  "ad_management-get_task": { method: "GET", path: "/api/tenants/{tenant_id}/ad-management-tasks/{task_id}", query: {"offset":"number","limit":"number"} },
  "ad_management-list_tasks": { method: "GET", path: "/api/tenants/{tenant_id}/ad-management-tasks", query: {"limit":"number"} },
  "ad_management-prepare_preview": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-previews", query: {} },
  "ad_management-prepare_restore": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-tasks/{task_id}/restore", query: {} },
  "ad_management-reconcile_task": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-tasks/{task_id}/reconcile", query: {} },
  "ad_management-resolve_rename_selection": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-rename-selections", query: {} },
  "ad_management-retry_targeted_refresh": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-tasks/{task_id}/refresh", query: {} },
  "ad_management-retry_task": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-tasks/{task_id}/retry", query: {} },
  "ad_management-submit_direct_task": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-requests", query: {} },
  "ad_management-submit_task": { method: "POST", path: "/api/tenants/{tenant_id}/ad-management-tasks", query: {} },
  "addBindings": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/mcp/connections/{connection_id}/bindings", query: {} },
  "ads_reporting-ad_detail": { method: "GET", path: "/api/tenants/{tenant_id}/ads/{kind}/{remote_id}", query: {} },
  "ads_reporting-ad_materials": { method: "GET", path: "/api/tenants/{tenant_id}/ads/ad/{remote_id}/materials", query: {} },
  "ads_reporting-ad_trend": { method: "GET", path: "/api/tenants/{tenant_id}/ads/trend", query: {} },
  "ads_reporting-create_report_export": { method: "POST", path: "/api/tenants/{tenant_id}/report-exports", query: {} },
  "ads_reporting-create_report_view": { method: "POST", path: "/api/tenants/{tenant_id}/report-views", query: {} },
  "ads_reporting-delete_report_view": { method: "DELETE", path: "/api/tenants/{tenant_id}/report-views/{view_id}", query: {} },
  "ads_reporting-download_report_export": { method: "GET", path: "/api/tenants/{tenant_id}/report-exports/{export_id}/download", query: {} },
  "ads_reporting-freeze_ad_material_selection": { method: "POST", path: "/api/tenants/{tenant_id}/ads/ad/{remote_id}/material-selections", query: {} },
  "ads_reporting-freeze_ad_selection": { method: "POST", path: "/api/tenants/{tenant_id}/ad-selections", query: {} },
  "ads_reporting-get_ad_sync_status": { method: "GET", path: "/api/tenants/{tenant_id}/ad-sync-status", query: {} },
  "ads_reporting-get_report_export": { method: "GET", path: "/api/tenants/{tenant_id}/report-exports/{export_id}", query: {} },
  "ads_reporting-list_report_exports": { method: "GET", path: "/api/tenants/{tenant_id}/report-exports", query: {} },
  "ads_reporting-list_report_views": { method: "GET", path: "/api/tenants/{tenant_id}/report-views", query: {} },
  "ads_reporting-metric_catalog": { method: "GET", path: "/api/tenants/{tenant_id}/ads/metric-catalog", query: {} },
  "ads_reporting-query_ads": { method: "GET", path: "/api/tenants/{tenant_id}/ads", query: {"min_spend":"number","max_spend":"number","min_d0_roas":"number","max_d0_roas":"number","min_target_roas":"number","max_target_roas":"number","include_empty":"boolean","limit":"number"} },
  "ads_reporting-request_ad_sync_refresh": { method: "POST", path: "/api/tenants/{tenant_id}/ad-sync-refresh", query: {} },
  "ads_reporting-update_report_view": { method: "PATCH", path: "/api/tenants/{tenant_id}/report-views/{view_id}", query: {} },
  "automation_rules-batch_rules": { method: "POST", path: "/api/tenants/{tenant_id}/automation-rules/batch", query: {} },
  "automation_rules-confirm_batch": { method: "POST", path: "/api/tenants/{tenant_id}/automation-confirmations/{confirmation_id}/confirm", query: {} },
  "automation_rules-create_rule": { method: "POST", path: "/api/tenants/{tenant_id}/automation-rules", query: {} },
  "automation_rules-delete_rule": { method: "DELETE", path: "/api/tenants/{tenant_id}/automation-rules/{rule_id}", query: {} },
  "automation_rules-dismiss_batch": { method: "POST", path: "/api/tenants/{tenant_id}/automation-confirmations/{confirmation_id}/dismiss", query: {} },
  "automation_rules-dry_run": { method: "POST", path: "/api/tenants/{tenant_id}/automation-rules/dry-run", query: {} },
  "automation_rules-get_catalog": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules/catalog", query: {} },
  "automation_rules-get_confirmation": { method: "GET", path: "/api/tenants/{tenant_id}/automation-confirmations/{confirmation_id}", query: {} },
  "automation_rules-get_overview": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules/overview", query: {} },
  "automation_rules-get_rule": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules/{rule_id}", query: {} },
  "automation_rules-get_run": { method: "GET", path: "/api/tenants/{tenant_id}/automation-runs/{run_id}", query: {} },
  "automation_rules-get_settings": { method: "GET", path: "/api/tenants/{tenant_id}/automation-settings", query: {} },
  "automation_rules-list_confirmations": { method: "GET", path: "/api/tenants/{tenant_id}/automation-confirmations", query: {} },
  "automation_rules-list_rules": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules", query: {} },
  "automation_rules-list_runs": { method: "GET", path: "/api/tenants/{tenant_id}/automation-runs", query: {"limit":"number","offset":"number"} },
  "automation_rules-list_templates": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules/templates", query: {} },
  "automation_rules-pick_accounts": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules/pickers/accounts", query: {"limit":"number"} },
  "automation_rules-pick_objects": { method: "GET", path: "/api/tenants/{tenant_id}/automation-rules/pickers/objects", query: {"advertiser_ids":"array","limit":"number","offset":"number"} },
  "automation_rules-put_settings": { method: "PUT", path: "/api/tenants/{tenant_id}/automation-settings", query: {} },
  "automation_rules-set_enabled": { method: "POST", path: "/api/tenants/{tenant_id}/automation-rules/{rule_id}/enabled", query: {} },
  "automation_rules-test_feishu": { method: "POST", path: "/api/tenants/{tenant_id}/automation-settings/test-feishu", query: {} },
  "automation_rules-update_rule": { method: "PUT", path: "/api/tenants/{tenant_id}/automation-rules/{rule_id}", query: {} },
  "availableBcs": { method: "GET", path: "/api/tenants/{tenant_id}/tiktok/mcp/connections/{connection_id}/available-bcs", query: {"page":"number","page_size":"number","refresh":"boolean"} },
  "builds-add_candidates": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/add", query: {} },
  "builds-adopt": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/adopt", query: {} },
  "builds-allocate": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/allocate", query: {} },
  "builds-apply": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/apply", query: {} },
  "builds-authorize_historical_read": { method: "POST", path: "/api/tenants/{tenant_id}/execution-steps/{step_id}/historical-read", query: {} },
  "builds-cancel": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/runs/{run_id}/cancel", query: {} },
  "builds-cancel_candidates": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/snapshots/{snapshot_id}/cancel", query: {} },
  "builds-candidate_progress": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/snapshots/{snapshot_id}", query: {} },
  "builds-candidates": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates", query: {"limit":"number"} },
  "builds-composition_state": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition", query: {} },
  "builds-create": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts", query: {} },
  "builds-differences": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/plans/{plan_id}/differences", query: {"limit":"number"} },
  "builds-dramas": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/dramas", query: {"limit":"number"} },
  "builds-edit": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/edits", query: {} },
  "builds-edit_groups": { method: "PATCH", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/dramas/{drama_id}/groups", query: {} },
  "builds-exclude": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/exclude", query: {} },
  "builds-frozen_ads": { method: "GET", path: "/api/tenants/{tenant_id}/build-units/{unit_id}/groups/{group_id}/ads", query: {"limit":"number"} },
  "builds-frozen_groups": { method: "GET", path: "/api/tenants/{tenant_id}/build-units/{unit_id}/groups", query: {"limit":"number"} },
  "builds-frozen_unit": { method: "GET", path: "/api/tenants/{tenant_id}/build-units/{unit_id}", query: {} },
  "builds-generate_preview": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/previews", query: {} },
  "builds-get_historical_read": { method: "GET", path: "/api/tenants/{tenant_id}/historical-build-reads/{read_id}", query: {} },
  "builds-get_historical_read_request": { method: "GET", path: "/api/tenants/{tenant_id}/historical-build-read-requests/{request_id}", query: {} },
  "builds-get_submission": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}", query: {} },
  "builds-get_submission_ads": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/units/{unit_id}/groups/{group_id}/ads", query: {"limit":"number"} },
  "builds-get_submission_events": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/events", query: {"limit":"number"} },
  "builds-get_submission_excluded": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/excluded", query: {"limit":"number"} },
  "builds-get_submission_groups": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/units/{unit_id}/groups", query: {"limit":"number"} },
  "builds-get_submission_materials": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/units/{unit_id}/groups/{group_id}/materials", query: {"limit":"number"} },
  "builds-get_submission_recovery": { method: "GET", path: "/api/tenants/{tenant_id}/submission-recoveries/{recovery_id}", query: {} },
  "builds-get_submission_steps": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/steps", query: {"limit":"number"} },
  "builds-get_submission_units": { method: "GET", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/units", query: {"limit":"number"} },
  "builds-identity_options": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/identities", query: {} },
  "builds-image_dramas": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/dramas", query: {"limit":"number"} },
  "builds-inputs": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/inputs", query: {"limit":"number"} },
  "builds-list_drafts": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts", query: {"limit":"number"} },
  "builds-list_submissions": { method: "GET", path: "/api/tenants/{tenant_id}/submissions", query: {"limit":"number"} },
  "builds-materials": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/dramas/{drama_id}/materials", query: {"limit":"number"} },
  "builds-minis_options": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/minis", query: {"page":"number"} },
  "builds-music_progress": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/jobs/{job_id}", query: {} },
  "builds-music_retry_batches": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/batch-retries", query: {} },
  "builds-music_retry_progress": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/batch-retries/{run_id}", query: {} },
  "builds-music_summary": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music", query: {} },
  "builds-plan": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/plans/{plan_id}", query: {} },
  "builds-plan_units": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/plans/{plan_id}/units", query: {"limit":"number"} },
  "builds-prepare": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/prepare", query: {} },
  "builds-preview": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/plans", query: {} },
  "builds-preview_dramas": { method: "GET", path: "/api/tenants/{tenant_id}/build-previews/{preview_id}/dramas", query: {"limit":"number"} },
  "builds-preview_inputs": { method: "GET", path: "/api/tenants/{tenant_id}/build-previews/{preview_id}/inputs", query: {"limit":"number","issues_only":"boolean"} },
  "builds-preview_request": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/previews/{revision}", query: {} },
  "builds-preview_summary": { method: "GET", path: "/api/tenants/{tenant_id}/build-previews/{preview_id}", query: {} },
  "builds-preview_units": { method: "GET", path: "/api/tenants/{tenant_id}/build-previews/{preview_id}/units", query: {"limit":"number"} },
  "builds-progress": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/runs/{run_id}", query: {} },
  "builds-put_manual_link": { method: "PUT", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/inputs/{input_id}/manual-link", query: {} },
  "builds-read_music": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/read", query: {} },
  "builds-reconcile_submission": { method: "POST", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/reconcile", query: {} },
  "builds-refresh": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/candidates/refresh", query: {} },
  "builds-refresh_identities_catalog": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/identities/refresh", query: {} },
  "builds-refresh_minis_catalog": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/minis/refresh", query: {} },
  "builds-request_music": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/units/{unit_id}", query: {} },
  "builds-retry_selected_music": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/batch-retries", query: {} },
  "builds-retry_submission": { method: "POST", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/retry", query: {} },
  "builds-saved_mutation": { method: "GET", path: "/api/tenants/{tenant_id}/build-mutation-requests/{request_id}", query: {} },
  "builds-saved_prepare_request": { method: "GET", path: "/api/tenants/{tenant_id}/build-preparation-requests/{request_id}", query: {} },
  "builds-saved_request": { method: "GET", path: "/api/tenants/{tenant_id}/build-draft-requests/{request_id}", query: {} },
  "builds-saved_submission": { method: "GET", path: "/api/tenants/{tenant_id}/submission-requests/{request_id}", query: {} },
  "builds-saved_submission_recovery": { method: "GET", path: "/api/tenants/{tenant_id}/submission-recovery-requests/{request_id}", query: {} },
  "builds-search_music": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/music/search", query: {} },
  "builds-select_identity": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/identities", query: {} },
  "builds-select_mini": { method: "POST", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/minis", query: {} },
  "builds-skipped_materials": { method: "GET", path: "/api/tenants/{tenant_id}/build-units/{unit_id}/skipped-materials", query: {"limit":"number"} },
  "builds-submit_preview": { method: "POST", path: "/api/tenants/{tenant_id}/build-previews/{preview_id}/submit", query: {} },
  "builds-summary": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}", query: {} },
  "builds-targeting_accounts": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/targeting/accounts", query: {"limit":"number"} },
  "builds-targeting_options": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/targeting", query: {} },
  "builds-targeting_regions": { method: "GET", path: "/api/tenants/{tenant_id}/targeting/regions", query: {} },
  "builds-units": { method: "GET", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/image-composition/units", query: {"limit":"number"} },
  "builds-update": { method: "PATCH", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}", query: {} },
  "builds-update_delivery": { method: "PATCH", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/delivery", query: {} },
  "builds-update_image_structure": { method: "PUT", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/structure", query: {} },
  "builds-update_targeting": { method: "PATCH", path: "/api/tenants/{tenant_id}/build-drafts/{draft_id}/targeting", query: {} },
  "business-dashboard": { method: "POST", path: "/api/tenants/{tenant_id}/business/query", query: {} },
  "business-download": { method: "GET", path: "/api/tenants/{tenant_id}/business/exports/{export_id}/download", query: {} },
  "business-dramas": { method: "GET", path: "/api/tenants/{tenant_id}/business/dramas", query: {"limit":"number"} },
  "business-evidence": { method: "PUT", path: "/api/tenants/{tenant_id}/business/evidence", query: {} },
  "business-export": { method: "POST", path: "/api/tenants/{tenant_id}/business/exports", query: {} },
  "business-export_status": { method: "GET", path: "/api/tenants/{tenant_id}/business/exports/{export_id}", query: {} },
  "business-mapping": { method: "PUT", path: "/api/tenants/{tenant_id}/business/mapping", query: {} },
  "business-member": { method: "PUT", path: "/api/tenants/{tenant_id}/business/member", query: {} },
  "business-options": { method: "GET", path: "/api/tenants/{tenant_id}/business/options", query: {} },
  "business-rates": { method: "GET", path: "/api/tenants/{tenant_id}/business/rates", query: {} },
  "business-read_evidence": { method: "GET", path: "/api/tenants/{tenant_id}/business/evidence", query: {} },
  "business-rebuild": { method: "POST", path: "/api/tenants/{tenant_id}/business/rebuild", query: {} },
  "business-runs": { method: "GET", path: "/api/tenants/{tenant_id}/business/runs", query: {} },
  "business-set_rate": { method: "POST", path: "/api/tenants/{tenant_id}/business/rates", query: {} },
  "cancel_ingest_file": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/cancel", query: {} },
  "complete_ingest_file": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/complete", query: {} },
  "create_ingest_chunk": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/chunks", query: {} },
  "create_ingest_generation": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/new-generation", query: {} },
  "create_ingest_session": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions", query: {} },
  "integrations-callback": { method: "GET", path: "/api/integrations/tiktok/mcp/callback", query: {} },
  "integrations-tiktok_callback": { method: "GET", path: "/api/integrations/tiktok/callback", query: {} },
  "list_ingest_files": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files", query: {"limit":"number"} },
  "list_ingest_parts": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/parts", query: {"generation":"number","operation_revision":"number","limit":"number"} },
  "list_ingest_sessions": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions", query: {"limit":"number"} },
  "login-login_access_token": { method: "POST", path: "/api/login/access-token", query: {} },
  "login-test_token": { method: "POST", path: "/api/login/test-token", query: {} },
  "material-production-get_material_production": { method: "GET", path: "/api/tenants/{tenant_id}/material-production", query: {} },
  "material-production-post_material_login_link": { method: "POST", path: "/api/tenants/{tenant_id}/material-production/login-link", query: {} },
  "material-production-retry_material_production": { method: "POST", path: "/api/platform/tenants/{tenant_id}/material-production/retry", query: {} },
  "material-push-get_external_source": { method: "GET", path: "/api/integrations/materials/source/{material_id}/{operation_id}", query: {"expires":"number"} },
  "material-push-get_material_batch": { method: "GET", path: "/api/integrations/materials/batches/{batch_id}", query: {} },
  "material-push-post_material_batch": { method: "POST", path: "/api/integrations/materials/batches", query: {} },
  "materials-authorize_reissue": { method: "POST", path: "/api/tenants/{tenant_id}/submissions/{submission_id}/material-reissues", query: {} },
  "materials-copy_version": { method: "POST", path: "/api/tenants/{tenant_id}/materials/carousel-presets/{version_id}/copy", query: {} },
  "materials-get_assets": { method: "GET", path: "/api/tenants/{tenant_id}/materials/{material_id}/assets", query: {"limit":"number"} },
  "materials-get_attempts": { method: "GET", path: "/api/tenants/{tenant_id}/materials/{material_id}/attempts", query: {"limit":"number"} },
  "materials-get_material": { method: "GET", path: "/api/tenants/{tenant_id}/materials/{material_id}", query: {} },
  "materials-get_materials": { method: "GET", path: "/api/tenants/{tenant_id}/materials", query: {"limit":"number"} },
  "materials-list_versions": { method: "GET", path: "/api/tenants/{tenant_id}/materials/carousel-presets", query: {"offset":"number","limit":"number"} },
  "materials-load_version": { method: "POST", path: "/api/tenants/{tenant_id}/materials/carousel-presets/{version_id}/load", query: {} },
  "materials-post_complete": { method: "POST", path: "/api/tenants/{tenant_id}/materials/{material_id}/complete", query: {} },
  "materials-post_object_retry": { method: "POST", path: "/api/tenants/{tenant_id}/materials/{material_id}/retry", query: {} },
  "materials-post_part_signature": { method: "POST", path: "/api/tenants/{tenant_id}/materials/{material_id}/upload-parts/{part_number}/sign", query: {} },
  "materials-post_upload_batch": { method: "POST", path: "/api/tenants/{tenant_id}/materials/upload-batches", query: {} },
  "materials-read_account_video_preview": { method: "GET", path: "/api/tenants/{tenant_id}/materials/account-video-preview", query: {} },
  "materials-read_original_preview": { method: "GET", path: "/api/tenants/{tenant_id}/materials/{material_id}/preview", query: {} },
  "materials-read_remote_preview": { method: "GET", path: "/api/tenants/{tenant_id}/materials/{material_id}/remote-preview", query: {} },
  "materials-read_upload_batch": { method: "GET", path: "/api/tenants/{tenant_id}/materials/upload-batches/{batch_id}", query: {} },
  "materials-read_upload_batches": { method: "GET", path: "/api/tenants/{tenant_id}/materials/upload-batches", query: {"limit":"number"} },
  "materials-read_upload_request": { method: "GET", path: "/api/tenants/{tenant_id}/materials/upload-requests/{request_id}", query: {} },
  "materials-read_version": { method: "GET", path: "/api/tenants/{tenant_id}/materials/carousel-presets/{version_id}", query: {} },
  "materials-save_version": { method: "POST", path: "/api/tenants/{tenant_id}/materials/carousel-presets", query: {} },
  "providers-application_capabilities": { method: "GET", path: "/api/tenants/{tenant_id}/providers/connections/{connection_id}/applications/{application_id}/capabilities", query: {} },
  "providers-get_links": { method: "GET", path: "/api/tenants/{tenant_id}/providers/links", query: {"limit":"number"} },
  "providers-get_preparation": { method: "GET", path: "/api/tenants/{tenant_id}/providers/link-preparations/{task_id}", query: {"page_size":"number","exceptions_only":"boolean"} },
  "providers-get_preparation_summary": { method: "GET", path: "/api/tenants/{tenant_id}/providers/link-preparations/{task_id}/summary", query: {} },
  "providers-link_details": { method: "GET", path: "/api/tenants/{tenant_id}/providers/links/{link_id}", query: {} },
  "providers-list_applications": { method: "GET", path: "/api/tenants/{tenant_id}/providers/connections/{connection_id}/applications", query: {"limit":"number"} },
  "providers-list_connections": { method: "GET", path: "/api/tenants/{tenant_id}/providers/connections", query: {"limit":"number"} },
  "providers-patch_application_link_defaults": { method: "PATCH", path: "/api/tenants/{tenant_id}/providers/connections/{connection_id}/applications/{application_id}/link-defaults", query: {} },
  "providers-patch_connection": { method: "PATCH", path: "/api/tenants/{tenant_id}/providers/connections/{connection_id}", query: {} },
  "providers-post_candidate": { method: "POST", path: "/api/tenants/{tenant_id}/providers/inputs/{input_id}/candidate", query: {} },
  "providers-post_connection": { method: "POST", path: "/api/tenants/{tenant_id}/providers/connections", query: {} },
  "providers-post_preparation": { method: "POST", path: "/api/tenants/{tenant_id}/providers/link-preparations", query: {} },
  "providers-post_verify": { method: "POST", path: "/api/tenants/{tenant_id}/providers/connections/{connection_id}/verify", query: {} },
  "read_ingest_chunk": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/chunks/{request_id}", query: {} },
  "read_ingest_file": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}", query: {} },
  "read_ingest_part_permissions": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/part-permissions", query: {"generation":"number","operation_revision":"number"} },
  "read_ingest_request": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-requests/{request_id}", query: {} },
  "read_ingest_summary": { method: "GET", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}", query: {} },
  "resume_ingest_file": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/resume", query: {} },
  "seal_ingest_session": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/seal", query: {} },
  "sign_ingest_parts": { method: "POST", path: "/api/tenants/{tenant_id}/materials/ingest-sessions/{session_id}/files/{material_id}/part-urls", query: {} },
  "strategies-append": { method: "POST", path: "/api/tenants/{tenant_id}/strategies/{strategy_id}/versions", query: {} },
  "strategies-copy_pool": { method: "GET", path: "/api/tenants/{tenant_id}/copy-pools/{version_id}", query: {} },
  "strategies-create": { method: "POST", path: "/api/tenants/{tenant_id}/strategies", query: {} },
  "strategies-get_one": { method: "GET", path: "/api/tenants/{tenant_id}/strategies/{strategy_id}", query: {} },
  "strategies-get_strategies": { method: "GET", path: "/api/tenants/{tenant_id}/strategies", query: {"active":"boolean","limit":"number"} },
  "strategies-saved_request": { method: "GET", path: "/api/tenants/{tenant_id}/strategy-save-requests/{request_id}", query: {} },
  "strategies-set_state": { method: "PATCH", path: "/api/tenants/{tenant_id}/strategies/{strategy_id}", query: {} },
  "strategies-structure_estimate": { method: "POST", path: "/api/tenants/{tenant_id}/strategies/structure-estimate", query: {} },
  "strategies-validate": { method: "POST", path: "/api/tenants/{tenant_id}/strategies/validate", query: {} },
  "strategies-version": { method: "GET", path: "/api/tenants/{tenant_id}/strategy-versions/{version_id}", query: {} },
  "strategies-versions": { method: "GET", path: "/api/tenants/{tenant_id}/strategies/{strategy_id}/versions", query: {"limit":"number"} },
  "syncApiConnection": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/connections/{connection_id}/sync", query: {} },
  "syncMcpBc": { method: "POST", path: "/api/tenants/{tenant_id}/tiktok/mcp/connections/{connection_id}/bcs/{bc_id}/sync", query: {} },
  "tenants-get_member_candidates": { method: "GET", path: "/api/tenants/{tenant_id}/member-candidates", query: {"limit":"number"} },
  "tenants-get_members": { method: "GET", path: "/api/tenants/{tenant_id}/members", query: {"limit":"number","active":"boolean"} },
  "tenants-get_my_tenants": { method: "GET", path: "/api/me/tenants", query: {"limit":"number","active":"boolean"} },
  "tenants-get_platform_tenants": { method: "GET", path: "/api/platform/tenants", query: {"limit":"number","active":"boolean"} },
  "tenants-get_platform_user_candidates": { method: "GET", path: "/api/platform/user-candidates", query: {"limit":"number"} },
  "tenants-patch_tenant": { method: "PATCH", path: "/api/platform/tenants/{tenant_id}", query: {} },
  "tenants-post_member_user": { method: "POST", path: "/api/tenants/{tenant_id}/members/users", query: {} },
  "tenants-post_tenant": { method: "POST", path: "/api/platform/tenants", query: {} },
  "tenants-put_member": { method: "PUT", path: "/api/tenants/{tenant_id}/members", query: {} },
  "unbindMcpBc": { method: "DELETE", path: "/api/tenants/{tenant_id}/tiktok/mcp/connections/{connection_id}/bcs/{bc_id}", query: {} },
  "users-create_user": { method: "POST", path: "/api/users/", query: {} },
  "users-delete_user": { method: "DELETE", path: "/api/users/{user_id}", query: {} },
  "users-delete_user_me": { method: "DELETE", path: "/api/users/me", query: {} },
  "users-read_user_by_id": { method: "GET", path: "/api/users/{user_id}", query: {} },
  "users-read_user_me": { method: "GET", path: "/api/users/me", query: {} },
  "users-read_users": { method: "GET", path: "/api/users/", query: {"skip":"number","limit":"number"} },
  "users-update_password_me": { method: "PATCH", path: "/api/users/me/password", query: {} },
  "users-update_user": { method: "PATCH", path: "/api/users/{user_id}", query: {} },
  "users-update_user_me": { method: "PATCH", path: "/api/users/me", query: {} },
  "utils-health_check": { method: "GET", path: "/api/utils/health-check/", query: {} },
} as const

export interface DemoOperationTypes {
  "accounts-authorize": { data: accountsAuthorizeData; response: accountsAuthorizeResponse }
  "accounts-binding": { data: accountsBindingData; response: accountsBindingResponse }
  "accounts-candidate_bcs": { data: accountsCandidateBcsData; response: accountsCandidateBcsResponse }
  "accounts-capability_status": { data: accountsCapabilityStatusData; response: accountsCapabilityStatusResponse }
  "accounts-configuration": { data: accountsConfigurationData; response: accountsConfigurationResponse }
  "accounts-disable": { data: accountsDisableData; response: accountsDisableResponse }
  "accounts-get_accounts": { data: accountsGetAccountsData; response: accountsGetAccountsResponse }
  "accounts-get_bcs": { data: accountsGetBcsData; response: accountsGetBcsResponse }
  "accounts-get_configuration": { data: accountsGetConfigurationData; response: accountsGetConfigurationResponse }
  "accounts-get_connections": { data: accountsGetConnectionsData; response: accountsGetConnectionsResponse }
  "accounts-patch_connection": { data: accountsPatchConnectionData; response: accountsPatchConnectionResponse }
  "accounts-post_authorization": { data: accountsPostAuthorizationData; response: accountsPostAuthorizationResponse }
  "accounts-post_resolve": { data: accountsPostResolveData; response: accountsPostResolveResponse }
  "accounts-put_default_connection": { data: accountsPutDefaultConnectionData; response: accountsPutDefaultConnectionResponse }
  "accounts-refresh_capabilities": { data: accountsRefreshCapabilitiesData; response: accountsRefreshCapabilitiesResponse }
  "accounts-revoke": { data: accountsRevokeData; response: accountsRevokeResponse }
  "acknowledge_ingest_parts": { data: acknowledgeIngestPartsData; response: acknowledgeIngestPartsResponse }
  "ad_management-cancel_task": { data: adManagementCancelTaskData; response: adManagementCancelTaskResponse }
  "ad_management-get_task": { data: adManagementGetTaskData; response: adManagementGetTaskResponse }
  "ad_management-list_tasks": { data: adManagementListTasksData; response: adManagementListTasksResponse }
  "ad_management-prepare_preview": { data: adManagementPreparePreviewData; response: adManagementPreparePreviewResponse }
  "ad_management-prepare_restore": { data: adManagementPrepareRestoreData; response: adManagementPrepareRestoreResponse }
  "ad_management-reconcile_task": { data: adManagementReconcileTaskData; response: adManagementReconcileTaskResponse }
  "ad_management-resolve_rename_selection": { data: adManagementResolveRenameSelectionData; response: adManagementResolveRenameSelectionResponse }
  "ad_management-retry_targeted_refresh": { data: adManagementRetryTargetedRefreshData; response: adManagementRetryTargetedRefreshResponse }
  "ad_management-retry_task": { data: adManagementRetryTaskData; response: adManagementRetryTaskResponse }
  "ad_management-submit_direct_task": { data: adManagementSubmitDirectTaskData; response: adManagementSubmitDirectTaskResponse }
  "ad_management-submit_task": { data: adManagementSubmitTaskData; response: adManagementSubmitTaskResponse }
  "addBindings": { data: addBindingsData; response: addBindingsResponse }
  "ads_reporting-ad_detail": { data: adsReportingAdDetailData; response: adsReportingAdDetailResponse }
  "ads_reporting-ad_materials": { data: adsReportingAdMaterialsData; response: adsReportingAdMaterialsResponse }
  "ads_reporting-ad_trend": { data: adsReportingAdTrendData; response: adsReportingAdTrendResponse }
  "ads_reporting-create_report_export": { data: adsReportingCreateReportExportData; response: adsReportingCreateReportExportResponse }
  "ads_reporting-create_report_view": { data: adsReportingCreateReportViewData; response: adsReportingCreateReportViewResponse }
  "ads_reporting-delete_report_view": { data: adsReportingDeleteReportViewData; response: adsReportingDeleteReportViewResponse }
  "ads_reporting-download_report_export": { data: adsReportingDownloadReportExportData; response: unknown }
  "ads_reporting-freeze_ad_material_selection": { data: adsReportingFreezeAdMaterialSelectionData; response: adsReportingFreezeAdMaterialSelectionResponse }
  "ads_reporting-freeze_ad_selection": { data: adsReportingFreezeAdSelectionData; response: adsReportingFreezeAdSelectionResponse }
  "ads_reporting-get_ad_sync_status": { data: adsReportingGetAdSyncStatusData; response: adsReportingGetAdSyncStatusResponse }
  "ads_reporting-get_report_export": { data: adsReportingGetReportExportData; response: adsReportingGetReportExportResponse }
  "ads_reporting-list_report_exports": { data: adsReportingListReportExportsData; response: adsReportingListReportExportsResponse }
  "ads_reporting-list_report_views": { data: adsReportingListReportViewsData; response: adsReportingListReportViewsResponse }
  "ads_reporting-metric_catalog": { data: adsReportingMetricCatalogData; response: adsReportingMetricCatalogResponse }
  "ads_reporting-query_ads": { data: adsReportingQueryAdsData; response: adsReportingQueryAdsResponse }
  "ads_reporting-request_ad_sync_refresh": { data: adsReportingRequestAdSyncRefreshData; response: adsReportingRequestAdSyncRefreshResponse }
  "ads_reporting-update_report_view": { data: adsReportingUpdateReportViewData; response: adsReportingUpdateReportViewResponse }
  "automation_rules-batch_rules": { data: automationRulesBatchRulesData; response: automationRulesBatchRulesResponse }
  "automation_rules-confirm_batch": { data: automationRulesConfirmBatchData; response: automationRulesConfirmBatchResponse }
  "automation_rules-create_rule": { data: automationRulesCreateRuleData; response: automationRulesCreateRuleResponse }
  "automation_rules-delete_rule": { data: automationRulesDeleteRuleData; response: automationRulesDeleteRuleResponse }
  "automation_rules-dismiss_batch": { data: automationRulesDismissBatchData; response: automationRulesDismissBatchResponse }
  "automation_rules-dry_run": { data: automationRulesDryRunData; response: automationRulesDryRunResponse }
  "automation_rules-get_catalog": { data: automationRulesGetCatalogData; response: automationRulesGetCatalogResponse }
  "automation_rules-get_confirmation": { data: automationRulesGetConfirmationData; response: automationRulesGetConfirmationResponse }
  "automation_rules-get_overview": { data: automationRulesGetOverviewData; response: automationRulesGetOverviewResponse }
  "automation_rules-get_rule": { data: automationRulesGetRuleData; response: automationRulesGetRuleResponse }
  "automation_rules-get_run": { data: automationRulesGetRunData; response: automationRulesGetRunResponse }
  "automation_rules-get_settings": { data: automationRulesGetSettingsData; response: automationRulesGetSettingsResponse }
  "automation_rules-list_confirmations": { data: automationRulesListConfirmationsData; response: automationRulesListConfirmationsResponse }
  "automation_rules-list_rules": { data: automationRulesListRulesData; response: automationRulesListRulesResponse }
  "automation_rules-list_runs": { data: automationRulesListRunsData; response: automationRulesListRunsResponse }
  "automation_rules-list_templates": { data: automationRulesListTemplatesData; response: automationRulesListTemplatesResponse }
  "automation_rules-pick_accounts": { data: automationRulesPickAccountsData; response: automationRulesPickAccountsResponse }
  "automation_rules-pick_objects": { data: automationRulesPickObjectsData; response: automationRulesPickObjectsResponse }
  "automation_rules-put_settings": { data: automationRulesPutSettingsData; response: automationRulesPutSettingsResponse }
  "automation_rules-set_enabled": { data: automationRulesSetEnabledData; response: automationRulesSetEnabledResponse }
  "automation_rules-test_feishu": { data: automationRulesTestFeishuData; response: automationRulesTestFeishuResponse }
  "automation_rules-update_rule": { data: automationRulesUpdateRuleData; response: automationRulesUpdateRuleResponse }
  "availableBcs": { data: availableBcsData; response: availableBcsResponse }
  "builds-add_candidates": { data: buildsAddCandidatesData; response: buildsAddCandidatesResponse }
  "builds-adopt": { data: buildsAdoptData; response: buildsAdoptResponse }
  "builds-allocate": { data: buildsAllocateData; response: buildsAllocateResponse }
  "builds-apply": { data: buildsApplyData; response: buildsApplyResponse }
  "builds-authorize_historical_read": { data: buildsAuthorizeHistoricalReadData; response: buildsAuthorizeHistoricalReadResponse }
  "builds-cancel": { data: buildsCancelData; response: buildsCancelResponse }
  "builds-cancel_candidates": { data: buildsCancelCandidatesData; response: buildsCancelCandidatesResponse }
  "builds-candidate_progress": { data: buildsCandidateProgressData; response: buildsCandidateProgressResponse }
  "builds-candidates": { data: buildsCandidatesData; response: buildsCandidatesResponse }
  "builds-composition_state": { data: buildsCompositionStateData; response: buildsCompositionStateResponse }
  "builds-create": { data: buildsCreateData; response: buildsCreateResponse }
  "builds-differences": { data: buildsDifferencesData; response: buildsDifferencesResponse }
  "builds-dramas": { data: buildsDramasData; response: buildsDramasResponse }
  "builds-edit": { data: buildsEditData; response: buildsEditResponse }
  "builds-edit_groups": { data: buildsEditGroupsData; response: buildsEditGroupsResponse }
  "builds-exclude": { data: buildsExcludeData; response: buildsExcludeResponse }
  "builds-frozen_ads": { data: buildsFrozenAdsData; response: buildsFrozenAdsResponse }
  "builds-frozen_groups": { data: buildsFrozenGroupsData; response: buildsFrozenGroupsResponse }
  "builds-frozen_unit": { data: buildsFrozenUnitData; response: buildsFrozenUnitResponse }
  "builds-generate_preview": { data: buildsGeneratePreviewData; response: buildsGeneratePreviewResponse }
  "builds-get_historical_read": { data: buildsGetHistoricalReadData; response: buildsGetHistoricalReadResponse }
  "builds-get_historical_read_request": { data: buildsGetHistoricalReadRequestData; response: buildsGetHistoricalReadRequestResponse }
  "builds-get_submission": { data: buildsGetSubmissionData; response: buildsGetSubmissionResponse }
  "builds-get_submission_ads": { data: buildsGetSubmissionAdsData; response: buildsGetSubmissionAdsResponse }
  "builds-get_submission_events": { data: buildsGetSubmissionEventsData; response: buildsGetSubmissionEventsResponse }
  "builds-get_submission_excluded": { data: buildsGetSubmissionExcludedData; response: buildsGetSubmissionExcludedResponse }
  "builds-get_submission_groups": { data: buildsGetSubmissionGroupsData; response: buildsGetSubmissionGroupsResponse }
  "builds-get_submission_materials": { data: buildsGetSubmissionMaterialsData; response: buildsGetSubmissionMaterialsResponse }
  "builds-get_submission_recovery": { data: buildsGetSubmissionRecoveryData; response: buildsGetSubmissionRecoveryResponse }
  "builds-get_submission_steps": { data: buildsGetSubmissionStepsData; response: buildsGetSubmissionStepsResponse }
  "builds-get_submission_units": { data: buildsGetSubmissionUnitsData; response: buildsGetSubmissionUnitsResponse }
  "builds-identity_options": { data: buildsIdentityOptionsData; response: buildsIdentityOptionsResponse }
  "builds-image_dramas": { data: buildsImageDramasData; response: buildsImageDramasResponse }
  "builds-inputs": { data: buildsInputsData; response: buildsInputsResponse }
  "builds-list_drafts": { data: buildsListDraftsData; response: buildsListDraftsResponse }
  "builds-list_submissions": { data: buildsListSubmissionsData; response: buildsListSubmissionsResponse }
  "builds-materials": { data: buildsMaterialsData; response: buildsMaterialsResponse }
  "builds-minis_options": { data: buildsMinisOptionsData; response: buildsMinisOptionsResponse }
  "builds-music_progress": { data: buildsMusicProgressData; response: buildsMusicProgressResponse }
  "builds-music_retry_batches": { data: buildsMusicRetryBatchesData; response: buildsMusicRetryBatchesResponse }
  "builds-music_retry_progress": { data: buildsMusicRetryProgressData; response: buildsMusicRetryProgressResponse }
  "builds-music_summary": { data: buildsMusicSummaryData; response: buildsMusicSummaryResponse }
  "builds-plan": { data: buildsPlanData; response: buildsPlanResponse }
  "builds-plan_units": { data: buildsPlanUnitsData; response: buildsPlanUnitsResponse }
  "builds-prepare": { data: buildsPrepareData; response: buildsPrepareResponse }
  "builds-preview": { data: buildsPreviewData; response: buildsPreviewResponse }
  "builds-preview_dramas": { data: buildsPreviewDramasData; response: buildsPreviewDramasResponse }
  "builds-preview_inputs": { data: buildsPreviewInputsData; response: buildsPreviewInputsResponse }
  "builds-preview_request": { data: buildsPreviewRequestData; response: buildsPreviewRequestResponse }
  "builds-preview_summary": { data: buildsPreviewSummaryData; response: buildsPreviewSummaryResponse }
  "builds-preview_units": { data: buildsPreviewUnitsData; response: buildsPreviewUnitsResponse }
  "builds-progress": { data: buildsProgressData; response: buildsProgressResponse }
  "builds-put_manual_link": { data: buildsPutManualLinkData; response: buildsPutManualLinkResponse }
  "builds-read_music": { data: buildsReadMusicData; response: buildsReadMusicResponse }
  "builds-reconcile_submission": { data: buildsReconcileSubmissionData; response: buildsReconcileSubmissionResponse }
  "builds-refresh": { data: buildsRefreshData; response: buildsRefreshResponse }
  "builds-refresh_identities_catalog": { data: buildsRefreshIdentitiesCatalogData; response: buildsRefreshIdentitiesCatalogResponse }
  "builds-refresh_minis_catalog": { data: buildsRefreshMinisCatalogData; response: buildsRefreshMinisCatalogResponse }
  "builds-request_music": { data: buildsRequestMusicData; response: buildsRequestMusicResponse }
  "builds-retry_selected_music": { data: buildsRetrySelectedMusicData; response: buildsRetrySelectedMusicResponse }
  "builds-retry_submission": { data: buildsRetrySubmissionData; response: buildsRetrySubmissionResponse }
  "builds-saved_mutation": { data: buildsSavedMutationData; response: buildsSavedMutationResponse }
  "builds-saved_prepare_request": { data: buildsSavedPrepareRequestData; response: buildsSavedPrepareRequestResponse }
  "builds-saved_request": { data: buildsSavedRequestData; response: buildsSavedRequestResponse }
  "builds-saved_submission": { data: buildsSavedSubmissionData; response: buildsSavedSubmissionResponse }
  "builds-saved_submission_recovery": { data: buildsSavedSubmissionRecoveryData; response: buildsSavedSubmissionRecoveryResponse }
  "builds-search_music": { data: buildsSearchMusicData; response: buildsSearchMusicResponse }
  "builds-select_identity": { data: buildsSelectIdentityData; response: buildsSelectIdentityResponse }
  "builds-select_mini": { data: buildsSelectMiniData; response: buildsSelectMiniResponse }
  "builds-skipped_materials": { data: buildsSkippedMaterialsData; response: buildsSkippedMaterialsResponse }
  "builds-submit_preview": { data: buildsSubmitPreviewData; response: buildsSubmitPreviewResponse }
  "builds-summary": { data: buildsSummaryData; response: buildsSummaryResponse }
  "builds-targeting_accounts": { data: buildsTargetingAccountsData; response: buildsTargetingAccountsResponse }
  "builds-targeting_options": { data: buildsTargetingOptionsData; response: buildsTargetingOptionsResponse }
  "builds-targeting_regions": { data: buildsTargetingRegionsData; response: buildsTargetingRegionsResponse }
  "builds-units": { data: buildsUnitsData; response: buildsUnitsResponse }
  "builds-update": { data: buildsUpdateData; response: buildsUpdateResponse }
  "builds-update_delivery": { data: buildsUpdateDeliveryData; response: buildsUpdateDeliveryResponse }
  "builds-update_image_structure": { data: buildsUpdateImageStructureData; response: buildsUpdateImageStructureResponse }
  "builds-update_targeting": { data: buildsUpdateTargetingData; response: buildsUpdateTargetingResponse }
  "business-dashboard": { data: businessDashboardData; response: businessDashboardResponse }
  "business-download": { data: businessDownloadData; response: unknown }
  "business-dramas": { data: businessDramasData; response: businessDramasResponse }
  "business-evidence": { data: businessEvidenceData; response: businessEvidenceResponse }
  "business-export": { data: businessExportData; response: businessExportResponse }
  "business-export_status": { data: businessExportStatusData; response: businessExportStatusResponse }
  "business-mapping": { data: businessMappingData; response: businessMappingResponse }
  "business-member": { data: businessMemberData; response: businessMemberResponse }
  "business-options": { data: businessOptionsData; response: businessOptionsResponse }
  "business-rates": { data: businessRatesData; response: businessRatesResponse }
  "business-read_evidence": { data: businessReadEvidenceData; response: businessReadEvidenceResponse }
  "business-rebuild": { data: businessRebuildData; response: businessRebuildResponse }
  "business-runs": { data: businessRunsData; response: businessRunsResponse }
  "business-set_rate": { data: businessSetRateData; response: businessSetRateResponse }
  "cancel_ingest_file": { data: cancelIngestFileData; response: cancelIngestFileResponse }
  "complete_ingest_file": { data: completeIngestFileData; response: completeIngestFileResponse }
  "create_ingest_chunk": { data: createIngestChunkData; response: createIngestChunkResponse }
  "create_ingest_generation": { data: createIngestGenerationData; response: createIngestGenerationResponse }
  "create_ingest_session": { data: createIngestSessionData; response: createIngestSessionResponse }
  "integrations-callback": { data: integrationsCallbackData; response: unknown }
  "integrations-tiktok_callback": { data: integrationsTiktokCallbackData; response: unknown }
  "list_ingest_files": { data: listIngestFilesData; response: listIngestFilesResponse }
  "list_ingest_parts": { data: listIngestPartsData; response: listIngestPartsResponse }
  "list_ingest_sessions": { data: listIngestSessionsData; response: listIngestSessionsResponse }
  "login-login_access_token": { data: loginLoginAccessTokenData; response: loginLoginAccessTokenResponse }
  "login-test_token": { data: loginTestTokenData; response: loginTestTokenResponse }
  "material-production-get_material_production": { data: materialProductionGetMaterialProductionData; response: materialProductionGetMaterialProductionResponse }
  "material-production-post_material_login_link": { data: materialProductionPostMaterialLoginLinkData; response: materialProductionPostMaterialLoginLinkResponse }
  "material-production-retry_material_production": { data: materialProductionRetryMaterialProductionData; response: materialProductionRetryMaterialProductionResponse }
  "material-push-get_external_source": { data: materialPushGetExternalSourceData; response: unknown }
  "material-push-get_material_batch": { data: materialPushGetMaterialBatchData; response: materialPushGetMaterialBatchResponse }
  "material-push-post_material_batch": { data: materialPushPostMaterialBatchData; response: materialPushPostMaterialBatchResponse }
  "materials-authorize_reissue": { data: materialsAuthorizeReissueData; response: materialsAuthorizeReissueResponse }
  "materials-copy_version": { data: materialsCopyVersionData; response: materialsCopyVersionResponse }
  "materials-get_assets": { data: materialsGetAssetsData; response: materialsGetAssetsResponse }
  "materials-get_attempts": { data: materialsGetAttemptsData; response: materialsGetAttemptsResponse }
  "materials-get_material": { data: materialsGetMaterialData; response: materialsGetMaterialResponse }
  "materials-get_materials": { data: materialsGetMaterialsData; response: materialsGetMaterialsResponse }
  "materials-list_versions": { data: materialsListVersionsData; response: materialsListVersionsResponse }
  "materials-load_version": { data: materialsLoadVersionData; response: materialsLoadVersionResponse }
  "materials-post_complete": { data: materialsPostCompleteData; response: materialsPostCompleteResponse }
  "materials-post_object_retry": { data: materialsPostObjectRetryData; response: materialsPostObjectRetryResponse }
  "materials-post_part_signature": { data: materialsPostPartSignatureData; response: materialsPostPartSignatureResponse }
  "materials-post_upload_batch": { data: materialsPostUploadBatchData; response: materialsPostUploadBatchResponse }
  "materials-read_account_video_preview": { data: materialsReadAccountVideoPreviewData; response: materialsReadAccountVideoPreviewResponse }
  "materials-read_original_preview": { data: materialsReadOriginalPreviewData; response: materialsReadOriginalPreviewResponse }
  "materials-read_remote_preview": { data: materialsReadRemotePreviewData; response: materialsReadRemotePreviewResponse }
  "materials-read_upload_batch": { data: materialsReadUploadBatchData; response: materialsReadUploadBatchResponse }
  "materials-read_upload_batches": { data: materialsReadUploadBatchesData; response: materialsReadUploadBatchesResponse }
  "materials-read_upload_request": { data: materialsReadUploadRequestData; response: materialsReadUploadRequestResponse }
  "materials-read_version": { data: materialsReadVersionData; response: materialsReadVersionResponse }
  "materials-save_version": { data: materialsSaveVersionData; response: materialsSaveVersionResponse }
  "providers-application_capabilities": { data: providersApplicationCapabilitiesData; response: providersApplicationCapabilitiesResponse }
  "providers-get_links": { data: providersGetLinksData; response: providersGetLinksResponse }
  "providers-get_preparation": { data: providersGetPreparationData; response: providersGetPreparationResponse }
  "providers-get_preparation_summary": { data: providersGetPreparationSummaryData; response: providersGetPreparationSummaryResponse }
  "providers-link_details": { data: providersLinkDetailsData; response: providersLinkDetailsResponse }
  "providers-list_applications": { data: providersListApplicationsData; response: providersListApplicationsResponse }
  "providers-list_connections": { data: providersListConnectionsData; response: providersListConnectionsResponse }
  "providers-patch_application_link_defaults": { data: providersPatchApplicationLinkDefaultsData; response: providersPatchApplicationLinkDefaultsResponse }
  "providers-patch_connection": { data: providersPatchConnectionData; response: providersPatchConnectionResponse }
  "providers-post_candidate": { data: providersPostCandidateData; response: providersPostCandidateResponse }
  "providers-post_connection": { data: providersPostConnectionData; response: providersPostConnectionResponse }
  "providers-post_preparation": { data: providersPostPreparationData; response: providersPostPreparationResponse }
  "providers-post_verify": { data: providersPostVerifyData; response: providersPostVerifyResponse }
  "read_ingest_chunk": { data: readIngestChunkData; response: readIngestChunkResponse }
  "read_ingest_file": { data: readIngestFileData; response: readIngestFileResponse }
  "read_ingest_part_permissions": { data: readIngestPartPermissionsData; response: readIngestPartPermissionsResponse }
  "read_ingest_request": { data: readIngestRequestData; response: readIngestRequestResponse }
  "read_ingest_summary": { data: readIngestSummaryData; response: readIngestSummaryResponse }
  "resume_ingest_file": { data: resumeIngestFileData; response: resumeIngestFileResponse }
  "seal_ingest_session": { data: sealIngestSessionData; response: sealIngestSessionResponse }
  "sign_ingest_parts": { data: signIngestPartsData; response: signIngestPartsResponse }
  "strategies-append": { data: strategiesAppendData; response: strategiesAppendResponse }
  "strategies-copy_pool": { data: strategiesCopyPoolData; response: strategiesCopyPoolResponse }
  "strategies-create": { data: strategiesCreateData; response: strategiesCreateResponse }
  "strategies-get_one": { data: strategiesGetOneData; response: strategiesGetOneResponse }
  "strategies-get_strategies": { data: strategiesGetStrategiesData; response: strategiesGetStrategiesResponse }
  "strategies-saved_request": { data: strategiesSavedRequestData; response: strategiesSavedRequestResponse }
  "strategies-set_state": { data: strategiesSetStateData; response: strategiesSetStateResponse }
  "strategies-structure_estimate": { data: strategiesStructureEstimateData; response: strategiesStructureEstimateResponse }
  "strategies-validate": { data: strategiesValidateData; response: strategiesValidateResponse }
  "strategies-version": { data: strategiesVersionData; response: strategiesVersionResponse }
  "strategies-versions": { data: strategiesVersionsData; response: strategiesVersionsResponse }
  "syncApiConnection": { data: syncApiConnectionData; response: syncApiConnectionResponse }
  "syncMcpBc": { data: syncMcpBcData; response: syncMcpBcResponse }
  "tenants-get_member_candidates": { data: tenantsGetMemberCandidatesData; response: tenantsGetMemberCandidatesResponse }
  "tenants-get_members": { data: tenantsGetMembersData; response: tenantsGetMembersResponse }
  "tenants-get_my_tenants": { data: tenantsGetMyTenantsData; response: tenantsGetMyTenantsResponse }
  "tenants-get_platform_tenants": { data: tenantsGetPlatformTenantsData; response: tenantsGetPlatformTenantsResponse }
  "tenants-get_platform_user_candidates": { data: tenantsGetPlatformUserCandidatesData; response: tenantsGetPlatformUserCandidatesResponse }
  "tenants-patch_tenant": { data: tenantsPatchTenantData; response: tenantsPatchTenantResponse }
  "tenants-post_member_user": { data: tenantsPostMemberUserData; response: tenantsPostMemberUserResponse }
  "tenants-post_tenant": { data: tenantsPostTenantData; response: tenantsPostTenantResponse }
  "tenants-put_member": { data: tenantsPutMemberData; response: tenantsPutMemberResponse }
  "unbindMcpBc": { data: unbindMcpBcData; response: unbindMcpBcResponse }
  "users-create_user": { data: usersCreateUserData; response: usersCreateUserResponse }
  "users-delete_user": { data: usersDeleteUserData; response: usersDeleteUserResponse }
  "users-delete_user_me": { data: usersDeleteUserMeData; response: usersDeleteUserMeResponse }
  "users-read_user_by_id": { data: usersReadUserByIdData; response: usersReadUserByIdResponse }
  "users-read_user_me": { data: usersReadUserMeData; response: usersReadUserMeResponse }
  "users-read_users": { data: usersReadUsersData; response: usersReadUsersResponse }
  "users-update_password_me": { data: usersUpdatePasswordMeData; response: usersUpdatePasswordMeResponse }
  "users-update_user": { data: usersUpdateUserData; response: usersUpdateUserResponse }
  "users-update_user_me": { data: usersUpdateUserMeData; response: usersUpdateUserMeResponse }
  "utils-health_check": { data: utilsHealthCheckData; response: utilsHealthCheckResponse }
}

export type DemoOperationId = keyof typeof DEMO_OPERATIONS
