#!/usr/bin/env bash
#
# OneDrive for Linux Client - Azure AD app registration (REQUIRED, once)
#
# This project ships with no Azure app identity baked in - every deployment
# registers its own. That's deliberate: a single Client ID shared by every
# installer would mean everyone's traffic runs through one person's Azure
# tenant forever (a single point of failure), and the consent screen every
# stranger sees would show that one person's identity instead of the
# software's own. Run this once, before first sign-in - it needs an account
# with Application Administrator or Global Administrator rights in whichever
# tenant you want the app registered in (your own tenant is fine even for
# personal Microsoft accounts; multi-tenant mode below still lets any
# account sign in afterward).
#
# On success this writes the resulting Client ID directly to this app's
# config file (respecting $XDG_CONFIG_HOME) - no manual editing of any
# source file needed, and the app picks it up on its next launch.
set -euo pipefail

DISPLAY_NAME="${1:-OneDrive Client for Linux}"
HOME_PAGE_URL="${2:-https://github.com/hasanaltin/OneDrive}"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/OneDrive"
CLIENT_ID_FILE="$CONFIG_DIR/client_id"

echo "==> Checking for Azure CLI"
if ! command -v az >/dev/null 2>&1; then
    echo "    not found - installing via Microsoft's official script (needs sudo)"
    curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
else
    echo "    found: $(az version --query '\"azure-cli\"' -o tsv 2>/dev/null || echo present)"
fi

echo "==> Signing in (this opens a browser - sign in with an account that has"
echo "    Application Administrator or Global Administrator rights in the"
echo "    target tenant)"
az login --allow-no-subscriptions

echo "==> Creating the app registration: \"$DISPLAY_NAME\""
APP_ID="$(az ad app create \
    --display-name "$DISPLAY_NAME" \
    --sign-in-audience AzureADMultipleOrgs \
    --is-fallback-public-client true \
    --web-home-page-url "$HOME_PAGE_URL" \
    --query appId -o tsv)"

echo ""
echo "Done. Application (client) ID:"
echo "    $APP_ID"
echo ""
echo "Remaining manual step (deliberately not scripted here, to avoid"
echo "guessing at Microsoft Graph permission GUIDs):"
echo "  1. https://entra.microsoft.com -> App registrations -> \"$DISPLAY_NAME\""
echo "  2. API permissions -> Add a permission -> Microsoft Graph -> Delegated"
echo "     -> add: Files.ReadWrite, Files.ReadWrite.All, People.Read, User.Read"
echo "  3. Click \"Grant admin consent\""
echo ""

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
printf '%s' "$APP_ID" > "$CLIENT_ID_FILE"
chmod 600 "$CLIENT_ID_FILE"

echo "Client ID written to $CLIENT_ID_FILE - once permissions are granted"
echo "(step 3 above), sign in from the app as usual."
