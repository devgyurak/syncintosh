# SPDX-License-Identifier: AGPL-3.0-or-later
"""The built-in presets for the other JetBrains IDEs: the common settings, with no product-specific `options/` file.

**Evidence: cask metadata and vendor pages read 2026-11-09** (no app of these products was installed, so no folder was
inspected; the file set is the one `presets/jetbrains.py` source-verified for every product, and DataGrip's real
folder in `presets/datagrip.py` is the format evidence):

```
Product      Bundle ID                 Folder prefix   Cask          Download page
CLion        com.jetbrains.CLion       CLion           clion         https://www.jetbrains.com/clion/download/
GoLand       com.jetbrains.goland      GoLand          goland        https://www.jetbrains.com/go/download/
IntelliJ     com.jetbrains.intellij    IntelliJIdea    intellij-idea https://www.jetbrains.com/idea/download/
PhpStorm     com.jetbrains.PhpStorm    PhpStorm        phpstorm      https://www.jetbrains.com/phpstorm/download/
PyCharm      com.jetbrains.pycharm     PyCharm         pycharm       https://www.jetbrains.com/pycharm/download/
RubyMine      com.jetbrains.rubymine   RubyMine        rubymine      https://www.jetbrains.com/ruby/download/
RustRover    com.jetbrains.rustrover   RustRover       rustrover     https://www.jetbrains.com/rust/download/
WebStorm     com.jetbrains.WebStorm    WebStorm        webstorm      https://www.jetbrains.com/webstorm/download/
```

Sources. **Bundle ID**: the cask's `uninstall quit:` id and its `~/Library/Preferences/<id>.plist` zap entry
(`https://formulae.brew.sh/api/cask/<token>.json`); the case is the vendor's (`CLion`, `PhpStorm`, `WebStorm` are
capitalised, `rustrover` and `rubymine` are not) and a wrong case would make "installed?" wrong. **Folder prefix**: the
cask zap path `~/Library/Application Support/JetBrains/<Prefix><year>.<n>` and the vendor's
`https://www.jetbrains.com/help/<product>/directories-used-by-the-ide-to-store-settings-caches-plugins-and-logs.html`
(`clion`, `idea`, `pycharm`, `phpstorm`, `webstorm`, `go`, `rust`, `ruby`). The RubyMine zap path omits the
`JetBrains/` segment and is stale; the help page confirms `JetBrains/RubyMine<ver>`. **Download page**: each URL
answered HTTP 200 without a redirect. RubyMine's page is `/ruby/download/` - `/rubymine/download/` redirects to a
landing page - and RustRover's is `/rust/download/`, GoLand's `/go/download/`.

None adds an `options/` file of its own. A product-specific file (`php.xml`, `php-tools.xml`, `twig.xml`,
`php-test-framework.xml` for PhpStorm; `gemmanager.xml` and `ruby-debugger-type-renderers-settings.xml` for RubyMine)
may roam, but the plugins are closed source, so no `@Storage` or `RoamingType` could be read and none is tracked. CLion
and RustRover have none to confirm. They stay a follow-up (`docs/design/presets.md`).

Not shipped: Rider (its `options/` layout, and where its `*.DotSettings` live, is unverified), the Community
editions (`IdeaIC`, `PyCharmCE`: their casks are deprecated) and Android Studio (Google's vendor folder).
"""

from __future__ import annotations

from syncintosh.core.presets import Preset
from syncintosh.presets.jetbrains import jetbrains_preset

CLION = jetbrains_preset("clion", "CLion", "com.jetbrains.CLion", "clion", "https://www.jetbrains.com/clion/download/")
GOLAND = jetbrains_preset(
    "goland", "GoLand", "com.jetbrains.goland", "goland", "https://www.jetbrains.com/go/download/"
)
INTELLIJ_IDEA = jetbrains_preset(
    "intellij-idea",
    "IntelliJIdea",
    "com.jetbrains.intellij",
    "intellij-idea",
    "https://www.jetbrains.com/idea/download/",
)
PHPSTORM = jetbrains_preset(
    "phpstorm", "PhpStorm", "com.jetbrains.PhpStorm", "phpstorm", "https://www.jetbrains.com/phpstorm/download/"
)
PYCHARM = jetbrains_preset(
    "pycharm", "PyCharm", "com.jetbrains.pycharm", "pycharm", "https://www.jetbrains.com/pycharm/download/"
)
RUBYMINE = jetbrains_preset(
    "rubymine", "RubyMine", "com.jetbrains.rubymine", "rubymine", "https://www.jetbrains.com/ruby/download/"
)
RUSTROVER = jetbrains_preset(
    "rustrover", "RustRover", "com.jetbrains.rustrover", "rustrover", "https://www.jetbrains.com/rust/download/"
)
WEBSTORM = jetbrains_preset(
    "webstorm", "WebStorm", "com.jetbrains.WebStorm", "webstorm", "https://www.jetbrains.com/webstorm/download/"
)

JETBRAINS_IDES: tuple[Preset, ...] = (CLION, GOLAND, INTELLIJ_IDEA, PHPSTORM, PYCHARM, RUBYMINE, RUSTROVER, WEBSTORM)

__all__ = [
    "CLION",
    "GOLAND",
    "INTELLIJ_IDEA",
    "JETBRAINS_IDES",
    "PHPSTORM",
    "PYCHARM",
    "RUBYMINE",
    "RUSTROVER",
    "WEBSTORM",
]
