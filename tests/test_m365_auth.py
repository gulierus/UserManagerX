"""
Tests for signing in to Microsoft 365 (Version 26, point 21 a).

The three methods differ in ways that decide whether multi-factor
authentication can work at all, so the differences are pinned here rather than
left to a comment.
"""

import pytest

from services.m365_auth import (
    APP_ONLY_SCOPES,
    DEFAULT_PUBLIC_CLIENT_ID,
    DELEGATED_SCOPES,
    AuthMethod,
    M365Credentials,
    build_credential,
    explain_auth_error,
)


def app_only(**overrides):
    values = dict(method=AuthMethod.APP_ONLY, tenant_id="tenant",
                  client_id="client", client_secret="secret")
    values.update(overrides)
    return M365Credentials(**values)


def ropc(**overrides):
    values = dict(method=AuthMethod.USERNAME_PASSWORD,
                  username="admin@skola.cz", password="secret")
    values.update(overrides)
    return M365Credentials(**values)


class TestTheMethods:
    """What each method is and is not."""

    @pytest.mark.parametrize("method", list(AuthMethod))
    def test_every_method_is_described(self, method):
        assert method.label and len(method.description) > 60

    def test_user_name_and_password_cannot_do_a_second_factor(self):
        # The ROPC flow has no step at which one could be requested.
        assert AuthMethod.USERNAME_PASSWORD.supports_mfa is False

    def test_the_device_code_flow_can(self):
        assert AuthMethod.DEVICE_CODE.supports_mfa is True

    def test_app_only_is_never_blocked_by_one(self):
        # There is no user to challenge.
        assert AuthMethod.APP_ONLY.supports_mfa is True

    def test_only_app_only_is_not_delegated(self):
        delegated = {m for m in AuthMethod if m.is_delegated}
        assert delegated == {AuthMethod.USERNAME_PASSWORD, AuthMethod.DEVICE_CODE}

    def test_the_description_of_the_password_method_names_the_limitation(self):
        assert "multi-factor" in AuthMethod.USERNAME_PASSWORD.description


class TestScopes:
    """What each method asks Microsoft for."""

    def test_the_delegated_methods_ask_for_the_four_permissions(self):
        assert ropc().scopes == DELEGATED_SCOPES
        assert M365Credentials(method=AuthMethod.DEVICE_CODE).scopes == \
            DELEGATED_SCOPES

    def test_app_only_asks_for_whatever_was_consented(self):
        assert app_only().scopes == APP_ONLY_SCOPES

    def test_the_four_permissions_are_the_ones_the_tool_needs(self):
        assert set(DELEGATED_SCOPES) == {
            "User.ReadWrite.All", "Group.ReadWrite.All",
            "UserAuthenticationMethod.ReadWrite.All", "Directory.ReadWrite.All",
        }


class TestDefaults:
    """What the user does not have to fill in."""

    def test_a_delegated_sign_in_falls_back_to_the_public_client(self):
        assert ropc().effective_client_id == DEFAULT_PUBLIC_CLIENT_ID

    def test_a_given_client_id_wins(self):
        assert ropc(client_id="ours").effective_client_id == "ours"

    def test_app_only_has_no_client_fallback(self):
        # A secret always belongs to a specific registration.
        assert app_only(client_id="").effective_client_id == ""

    def test_a_delegated_sign_in_falls_back_to_organizations(self):
        assert ropc().effective_tenant_id == "organizations"

    def test_app_only_must_name_its_tenant(self):
        assert app_only(tenant_id="").effective_tenant_id == ""

    def test_surrounding_whitespace_is_ignored(self):
        assert ropc(client_id="  ours  ").effective_client_id == "ours"


class TestProblems:
    """What is checked before anything is sent to Microsoft."""

    def test_complete_app_only_credentials_have_no_problems(self):
        assert app_only().problems() == []

    def test_complete_password_credentials_have_no_problems(self):
        assert ropc().problems() == []

    def test_the_device_code_method_needs_nothing(self):
        assert M365Credentials(method=AuthMethod.DEVICE_CODE).problems() == []

    @pytest.mark.parametrize("missing", ["tenant_id", "client_id",
                                         "client_secret"])
    def test_app_only_names_each_missing_field(self, missing):
        problems = app_only(**{missing: ""}).problems()
        assert len(problems) == 1

    @pytest.mark.parametrize("missing", ["username", "password"])
    def test_the_password_method_names_each_missing_field(self, missing):
        assert len(ropc(**{missing: ""}).problems()) == 1

    def test_several_missing_fields_are_all_reported(self):
        assert len(app_only(tenant_id="", client_id="",
                            client_secret="").problems()) == 3


class TestWarnings:
    """Things that are allowed but will surprise."""

    def test_the_password_method_warns_about_multi_factor(self):
        warnings = ropc().warnings()
        assert len(warnings) == 1 and "AADSTS50076" in warnings[0]

    def test_app_only_warns_about_password_resets(self):
        assert any("password" in warning.lower()
                   for warning in app_only().warnings())

    def test_the_device_code_method_needs_no_warning(self):
        assert M365Credentials(method=AuthMethod.DEVICE_CODE).warnings() == []


class TestRedaction:
    """Nothing secret may reach a log or a saved source."""

    #: A value that cannot occur anywhere else, so finding it proves a leak.
    SECRET = "s3cr3t-do-not-leak"

    def test_the_client_secret_never_appears(self):
        credentials = app_only(client_secret=self.SECRET)
        assert self.SECRET not in str(credentials.redacted())

    def test_the_password_never_appears(self):
        assert self.SECRET not in str(ropc(password=self.SECRET).redacted())

    def test_the_description_carries_no_secret(self):
        assert self.SECRET not in app_only(client_secret=self.SECRET).describe()
        assert self.SECRET not in ropc(password=self.SECRET).describe()

    def test_the_useful_facts_are_kept(self):
        redacted = app_only().redacted()
        assert redacted['tenant_id'] == "tenant"
        assert redacted['client_id'] == "client"
        assert redacted['auth_method'] == "app_only"

    def test_the_signed_in_account_is_kept(self):
        assert ropc().redacted()['username'] == "admin@skola.cz"


class TestBuildCredential:
    """The real azure-identity objects."""

    def test_app_only_builds_a_client_secret_credential(self):
        assert type(build_credential(app_only())).__name__ == \
            "ClientSecretCredential"

    def test_the_password_method_builds_a_username_password_credential(self):
        assert type(build_credential(ropc())).__name__ == \
            "UsernamePasswordCredential"

    def test_the_device_code_method_builds_a_device_code_credential(self):
        assert type(build_credential(
            M365Credentials(method=AuthMethod.DEVICE_CODE))).__name__ == \
            "DeviceCodeCredential"

    def test_incomplete_credentials_are_refused_before_any_request(self):
        with pytest.raises(ValueError, match="Tenant ID"):
            build_credential(app_only(tenant_id=""))


class TestExplainAuthError:
    """Microsoft's codes turned into something actionable."""

    @pytest.mark.parametrize("code,needle", [
        ("AADSTS50076", "Device code"),
        ("AADSTS50079", "Device code"),
        ("AADSTS50126", "wrong"),
        ("AADSTS7000215", "client secret"),
        ("AADSTS65001", "admin consent"),
        ("AADSTS53003", "Conditional Access"),
        ("AADSTS90002", "Tenant ID"),
        ("Authorization_RequestDenied", "permission"),
        ("unsupported_grant_type", "public client flows"),
    ])
    def test_the_known_codes_are_translated(self, code, needle):
        explained = explain_auth_error(Exception(f"{code}: raw text"))
        assert needle in explained

    def test_the_original_message_is_always_kept(self):
        explained = explain_auth_error(Exception("AADSTS50076: raw text"))
        assert "raw text" in explained

    def test_an_unknown_error_is_passed_through_unchanged(self):
        assert explain_auth_error(Exception("something else")) == "something else"
