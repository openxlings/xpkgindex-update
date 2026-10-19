package = {
    spec = "1",
    namespace = "xim",
    name = "foo",
    repo = "https://github.com/acme/foo",
    extensions = {
        autoupdate = {
            tag = "v${version}",
            url = "https://github.com/acme/foo/archive/refs/tags/${tag}.tar.gz",
        },
    },
    xpm = {
        linux = {
            ["latest"] = { ref = "1.0.0" },
            ["1.0.0"] = {
                url = "https://github.com/acme/foo/archive/refs/tags/v1.0.0.tar.gz",
                sha256 = "published-hash",
            },
        },
    },
}
