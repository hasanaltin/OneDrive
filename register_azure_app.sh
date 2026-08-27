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
    if ! curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash; then
        # Microsoft's apt repo doesn't always have packages for a brand-new
        # distro release codename yet ("E: Unable to locate package
        # azure-cli" - confirmed live on a fresh install of a just-released
        # Ubuntu version) - falls back to a dedicated venv + pip install,
        # which only needs PyPI, not a matching apt release.
        echo "    Microsoft's apt-based installer failed - likely means this distro" >&2
        echo "    release is too new for their package repo yet. Falling back to a" >&2
        echo "    dedicated pip install instead." >&2
        AZ_VENV="${XDG_DATA_HOME:-$HOME/.local/share}/onedrive-native-az-cli"
        python3 -m venv "$AZ_VENV"
        "$AZ_VENV/bin/pip" install --quiet --upgrade pip
        "$AZ_VENV/bin/pip" install --quiet azure-cli
        export PATH="$AZ_VENV/bin:$PATH"
    fi
fi
if ! command -v az >/dev/null 2>&1; then
    echo "Azure CLI still not found - install it manually, then re-run this script:" >&2
    echo "  https://learn.microsoft.com/cli/azure/install-azure-cli-linux" >&2
    exit 1
fi
echo "    using: $(az version --query '\"azure-cli\"' -o tsv 2>/dev/null || echo present)"

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

MANUAL_STEPS_NEEDED=0
GRAPH_API_ID="00000003-0000-0000-c000-000000000000"

echo "==> Adding required Microsoft Graph API permissions"
# Permission names are resolved to their GUIDs via a live lookup against the
# Microsoft Graph service principal itself - the same data the portal's "Add
# a permission" search box uses - rather than hardcoding GUIDs from memory,
# which would risk silently requesting the wrong scope.
API_PERMISSIONS=""
for PERM_NAME in Files.ReadWrite Files.ReadWrite.All People.Read User.Read; do
    PERM_ID="$(az ad sp show --id "$GRAPH_API_ID" \
        --query "oauth2PermissionScopes[?value=='$PERM_NAME'].id | [0]" -o tsv 2>/dev/null || true)"
    if [ -z "$PERM_ID" ] || [ "$PERM_ID" = "None" ]; then
        echo "    could not resolve '$PERM_NAME' automatically" >&2
        MANUAL_STEPS_NEEDED=1
        continue
    fi
    API_PERMISSIONS="$API_PERMISSIONS ${PERM_ID}=Scope"
done

if [ -n "$API_PERMISSIONS" ]; then
    # shellcheck disable=SC2086
    if ! az ad app permission add --id "$APP_ID" --api "$GRAPH_API_ID" --api-permissions $API_PERMISSIONS >/dev/null; then
        echo "    failed to add permissions automatically" >&2
        MANUAL_STEPS_NEEDED=1
    fi
else
    MANUAL_STEPS_NEEDED=1
fi

if [ "$MANUAL_STEPS_NEEDED" -eq 0 ]; then
    echo "==> Granting admin consent"
    # A freshly-added permission can take a few seconds to propagate before
    # admin-consent sees it, so retry a couple of times before giving up.
    CONSENT_OK=0
    for ATTEMPT in 1 2 3; do
        if az ad app permission admin-consent --id "$APP_ID" 2>/dev/null; then
            CONSENT_OK=1
            break
        fi
        sleep 5
    done
    if [ "$CONSENT_OK" -eq 0 ]; then
        echo "    admin consent failed - grant it manually (see below)" >&2
        MANUAL_STEPS_NEEDED=1
    fi
fi

echo ""
if [ "$MANUAL_STEPS_NEEDED" -eq 1 ]; then
    echo "One or more steps above couldn't be done automatically - finish them"
    echo "manually:"
    echo "  1. https://entra.microsoft.com -> App registrations -> \"$DISPLAY_NAME\""
    echo "  2. API permissions -> Add a permission -> Microsoft Graph -> Delegated"
    echo "     -> add whichever of these are still missing: Files.ReadWrite,"
    echo "        Files.ReadWrite.All, People.Read, User.Read"
    echo "  3. Click \"Grant admin consent\""
    echo ""
else
    echo "All required Microsoft Graph permissions were added and admin-consented"
    echo "automatically - no portal step needed."
    echo ""
fi

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
printf '%s' "$APP_ID" > "$CLIENT_ID_FILE"
chmod 600 "$CLIENT_ID_FILE"

echo "Client ID written to $CLIENT_ID_FILE."
if [ "$MANUAL_STEPS_NEEDED" -eq 1 ]; then
    echo "Once the manual step(s) above are done, sign in from the app as usual."
else
    echo "Sign in from the app as usual."
fi
