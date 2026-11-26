# To-do

- [ ] **Suppress stale iOS relay alerts.** Request Apple’s filtering entitlement for the
  Notification Service Extension and add it to the signed extension once approved.
  Return empty notification content when the home server confirms suppression with HTTP 204.
  For an alert already displayed, remove that specific notification the next time the app runs.
  Verify on a device that suppressed alerts leave no entry and allowed alerts still appear.
  [Apple’s entitlement documentation](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.usernotifications.filtering)
