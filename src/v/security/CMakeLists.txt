v_cc_library(
  NAME security_config
  SRCS
    config_bsl.cc
    config_rcl.cc
  DEPS
    v::json
    Seastar::seastar
    re2
    ada
)

v_cc_library(
  NAME security
  SRCS
    scram_algorithm.cc
    scram_authenticator.cc
    gssapi_authenticator.cc
    oidc_authenticator.cc
    oidc_principal_mapping.cc
    oidc_service.cc
    credential.cc
    request_auth.cc
    acl.cc
    mtls.cc
    license.cc
    krb5.cc
    krb5_configurator.cc
    gssapi_principal_mapper.cc
  DEPS
    v::http
    v::security_config
    v::config
    v::bytes
    v::utils
    v::rprandom
    v::version
    absl::flat_hash_map
    absl::flat_hash_set
    cryptopp
    re2
    gssapi_krb5
    krb5
 )

add_subdirectory(audit)
add_subdirectory(tests)
