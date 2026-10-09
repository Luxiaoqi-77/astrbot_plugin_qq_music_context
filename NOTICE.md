# Source provenance and license

## Local project code adapted

This package is not a clean-room rewrite. Its public-facts logic adapts a previous local version of this project's QQMusicLiteParser. The project owner confirms that this parser was originally added in an earlier task, rather than copied from the upstream author's public source. This package does not attribute that parser file to aliveriver.

The public upstream master currently has no lite_link_parser/parsers/qqmusic.py file, and GitHub reports no commit history for that exact path:

- [Current upstream parser directory](https://github.com/aliveriver/astrbot_plugin_continuous_message/tree/master/lite_link_parser/parsers)
- [History for the exact QQ Music parser path](https://github.com/aliveriver/astrbot_plugin_continuous_message/commits/master/lite_link_parser/parsers/qqmusic.py)

The four QQ Music-specific MessageParser helper names used by the previous local implementation are also absent from the current public master message_parser.py. Local task artifacts contain the parser, but those source snapshots have no Git metadata, so they do not provide a source commit SHA.

The derivative relationships in this package are:

- public_facts.py: adapts the earlier local QQMusicLiteParser's identity cleanup and normalization, QQ URL and song ID checks, source/song matching, genre/theme/intro extraction, cache key, search orchestration, and asynchronous lookup coordination. The standalone version adds stricter identity validation and source allowlists, separates work-level and recording-level evidence, keeps bounded evidence, and leaves unsupported fields unknown.
- card_parser.py: adapts QQ Music-specific card field selection, text sanitation, and URL/MID recognition from the earlier local MessageParser helpers. It adds bounded segment traversal, quote exclusion, per-event caps, and duplicate-card suppression.
- main.py: standalone event-context handling was authored for this plugin.
- The optional debounce bridge patch is separate and is not included in this package.

## Upstream license evidence

The upstream project currently has conflicting public license signals: the root [LICENSE](https://github.com/aliveriver/astrbot_plugin_continuous_message/blob/master/LICENSE) contains GNU AGPL version 3, while the [README](https://github.com/aliveriver/astrbot_plugin_continuous_message/blob/master/README.md) says “MIT License.” The repository page labels the project AGPL-3.0. The repository has zero Git tags; the README and metadata identify v2.9.1 on master, but no v2.9.1 tag was found.

This evidence does not make the local QQMusicLiteParser an upstream author's file. The project owner selected GNU AGPL-3.0 for this standalone plugin; see [LICENSE](LICENSE). This is an explicit license choice for the standalone package, not a conclusion drawn from the conflicting upstream README and root license. The optional debounce bridge patch adapts upstream consumer code and is excluded from this release pending separate review of its redistribution terms.
