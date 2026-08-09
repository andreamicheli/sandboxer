# Keep the control plane outside runners

Le chiamate ai provider, le credenziali, la gestione delle fasi, la rete e la verifica appartengono a un Orchestrator fidato esterno ai Runner. I Runner ricevono soltanto il toy service e tool limitati: questa separazione riduce il blast radius, impedisce ai modelli di accedere alle chiavi e rende verificabile l'apertura della connettività solo nella Red Phase.
