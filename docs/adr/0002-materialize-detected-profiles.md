# Materialize Detected Profiles into Project Configuration

Stack Detection produces explicit project configuration, with its Profile sources and generator version recorded, instead of loading changing Profile defaults during every run. This keeps generated behavior reviewable and reproducible while allowing deliberate `diff` and `refresh` operations when Profile definitions improve.
