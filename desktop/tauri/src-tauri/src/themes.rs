use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::Path,
};

use serde::Serialize;

const MAX_THEME_BYTES: u64 = 256 * 1024;

#[derive(Serialize)]
pub struct Theme {
    id: String,
    name: String,
    description: String,
    base: String,
}

#[derive(Serialize)]
pub struct Catalog {
    themes: Vec<Theme>,
    css: Option<String>,
    skipped: Vec<String>,
}

pub fn initialize(directory: &Path) -> io::Result<()> {
    fs::create_dir_all(directory)?;
    if directory.join(".defaults-installed").exists() {
        return Ok(());
    }
    for (name, css) in [
        ("Light.theme.css", include_str!("../themes/Light.theme.css")),
        ("OLED.theme.css", include_str!("../themes/OLED.theme.css")),
        (
            "Cyberpunk Midnight.theme.css",
            include_str!("../themes/Cyberpunk Midnight.theme.css"),
        ),
    ] {
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(directory.join(name))
        {
            Ok(mut file) => file.write_all(css.as_bytes())?,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
            Err(error) => return Err(error),
        }
    }
    fs::write(directory.join(".defaults-installed"), b"")
}

fn metadata<'a>(css: &'a str, key: &str) -> Option<&'a str> {
    let header = css.trim_start().strip_prefix("/*")?.split_once("*/")?.0;
    header.lines().find_map(|line| {
        let line = line.trim().trim_start_matches('*').trim();
        line.strip_prefix(key)
            .and_then(|value| value.strip_prefix(' '))
            .map(str::trim)
            .filter(|value| !value.is_empty())
    })
}

pub fn scan(directory: &Path, selected: &str) -> io::Result<Catalog> {
    let mut catalog = Catalog {
        themes: Vec::new(),
        css: None,
        skipped: Vec::new(),
    };
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let Some(id) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if !id.ends_with(".theme.css") {
            continue;
        }
        // Never follow theme symlinks or use a frontend-supplied filesystem path.
        if !entry.file_type()?.is_file() {
            catalog.skipped.push(id);
            continue;
        }
        let mut css = String::new();
        let result = File::open(entry.path())
            .and_then(|file| file.take(MAX_THEME_BYTES + 1).read_to_string(&mut css));
        if result.is_err() || css.len() as u64 > MAX_THEME_BYTES || css.trim().is_empty() {
            catalog.skipped.push(id);
            continue;
        }
        let base = metadata(&css, "@base").unwrap_or("dark");
        if !matches!(base, "light" | "dark" | "system") {
            catalog.skipped.push(id);
            continue;
        }
        catalog.themes.push(Theme {
            name: metadata(&css, "@name")
                .unwrap_or(id.trim_end_matches(".theme.css"))
                .to_owned(),
            description: metadata(&css, "@description").unwrap_or("").to_owned(),
            base: base.to_owned(),
            id: id.clone(),
        });
        if id == selected {
            catalog.css = Some(css);
        }
    }
    catalog
        .themes
        .sort_by(|a, b| a.name.cmp(&b.name).then(a.id.cmp(&b.id)));
    catalog.skipped.sort();
    Ok(catalog)
}

pub fn open_folder(directory: &Path) -> io::Result<()> {
    #[cfg(target_os = "windows")]
    {
        // Explorer can delegate to an existing process; its exit status does not
        // reliably describe whether that process opened the folder.
        std::process::Command::new("explorer.exe")
            .arg(directory)
            .spawn()
            .map(|_| ())
    }
    #[cfg(unix)]
    {
        #[cfg(target_os = "macos")]
        let mut command = std::process::Command::new("open");
        #[cfg(not(target_os = "macos"))]
        let mut command = std::process::Command::new("xdg-open");
        let status = command.arg(directory).status()?;
        if status.success() {
            Ok(())
        } else {
            Err(io::Error::other(
                "The file manager could not open the themes folder",
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn themes_are_discovered_reloaded_and_removed_without_overwriting_edits() -> io::Result<()> {
        let directory = std::env::temp_dir().join(format!("kaede-themes-{}", uuid::Uuid::new_v4()));
        initialize(&directory)?;
        assert_eq!(scan(&directory, "")?.themes.len(), 3);
        let name = "OLED.theme.css";
        let edited = "/**\n * @name Edited\n * @base light\n */\n:root { --accent: red; }";
        fs::write(directory.join(name), edited)?;
        initialize(&directory)?;
        let catalog = scan(&directory, name)?;
        assert_eq!(catalog.css.as_deref(), Some(edited));
        assert!(
            catalog
                .themes
                .iter()
                .any(|theme| theme.name == "Edited" && theme.base == "light")
        );
        fs::write(directory.join("New.theme.css"), ":root { --accent: blue; }")?;
        assert_eq!(scan(&directory, "")?.themes.len(), 4);
        fs::remove_file(directory.join(name))?;
        initialize(&directory)?;
        assert!(scan(&directory, name)?.css.is_none());
        assert!(scan(&directory, "../outside.theme.css")?.css.is_none());
        fs::write(
            directory.join("Large.theme.css"),
            vec![b'a'; MAX_THEME_BYTES as usize + 1],
        )?;
        fs::write(directory.join("Invalid.theme.css"), [0xff])?;
        #[cfg(unix)]
        std::os::unix::fs::symlink(
            directory.join("Light.theme.css"),
            directory.join("Link.theme.css"),
        )?;
        let catalog = scan(&directory, "Large.theme.css")?;
        assert!(catalog.css.is_none());
        assert!(catalog.skipped.contains(&"Large.theme.css".to_owned()));
        assert!(catalog.skipped.contains(&"Invalid.theme.css".to_owned()));
        #[cfg(unix)]
        assert!(catalog.skipped.contains(&"Link.theme.css".to_owned()));
        fs::remove_dir_all(directory)
    }
}
