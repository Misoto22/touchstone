# Make Actions Setup Resumable without Persisting Private Keys

Actions setup discovers previously completed file, App, installation, and secret steps so a Partial Setup can continue safely. A manifest-provided App private key remains in memory while secret storage is retried; if interruption loses that one-time key, doctor directs the owner to generate a replacement in GitHub rather than persisting plaintext key material or deleting the App.
