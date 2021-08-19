# Keycloak LDAPs

1. connect to docker container (or use keytool locally)

```docker-compose exec keycloak /bin/bash````

2. go to directory

`cd /opt/jboss/keycloak/standalone/configuration/customcerts/`

3. get certificate

from URI

`openssl s_client -showcerts -connect <LDAP_SERVER> </dev/null > customca.crt`

or copy file to `customcerts` mount-point

4. import certificate

`keytool -importcert -keystore /opt/jboss/keycloak/standalone/configuration/keystores/customca.jks -file /opt/jboss/keycloak/standalone/configuration/customcerts/<CERT_FILE>.crt -alias <CERT_SHORT_NAME>`
