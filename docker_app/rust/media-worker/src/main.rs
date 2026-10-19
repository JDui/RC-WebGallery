use anyhow::{bail, Context, Result};
use image::imageops::FilterType;
use image::{DynamicImage, GenericImageView, ImageFormat};
use serde::Serialize;
use std::env;
use std::fs;
use std::io::Cursor;
use std::path::{Path, PathBuf};

#[derive(Serialize)]
struct ImageResult {
    ok: bool,
    source: String,
    width: u32,
    height: u32,
    thumb: Option<String>,
    small: Option<String>,
}

fn main() {
    if let Err(error) = run() {
        println!(
            "{}",
            serde_json::json!({
                "ok": false,
                "error": error.to_string(),
            })
        );
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("probe-image") => {
            let source = arg_path(&args, "--source")?;
            let image = image::open(&source).with_context(|| format!("open image {}", source.display()))?;
            let (width, height) = image.dimensions();
            print_json(&ImageResult {
                ok: true,
                source: source.display().to_string(),
                width,
                height,
                thumb: None,
                small: None,
            })
        }
        Some("derive-image") => {
            let source = arg_path(&args, "--source")?;
            let thumb = arg_path(&args, "--thumb")?;
            let small = arg_path(&args, "--small")?;
            let image = image::open(&source).with_context(|| format!("open image {}", source.display()))?;
            let (width, height) = image.dimensions();
            write_resized_webp(&image, &thumb, 576)?;
            write_resized_webp(&image, &small, 192)?;
            print_json(&ImageResult {
                ok: true,
                source: source.display().to_string(),
                width,
                height,
                thumb: Some(thumb.display().to_string()),
                small: Some(small.display().to_string()),
            })
        }
        _ => bail!("usage: media-worker probe-image --source PATH | derive-image --source PATH --thumb PATH --small PATH"),
    }
}

fn arg_path(args: &[String], name: &str) -> Result<PathBuf> {
    args.windows(2)
        .find(|pair| pair[0] == name)
        .map(|pair| PathBuf::from(&pair[1]))
        .with_context(|| format!("missing argument {name}"))
}

fn write_resized_webp(image: &DynamicImage, target: &Path, short_edge: u32) -> Result<()> {
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    let (width, height) = image.dimensions();
    let current_short = width.min(height).max(1);
    let resized = if current_short <= short_edge {
        image.clone()
    } else {
        let scale = short_edge as f32 / current_short as f32;
        let next_width = ((width as f32 * scale).round() as u32).max(1);
        let next_height = ((height as f32 * scale).round() as u32).max(1);
        image.resize(next_width, next_height, FilterType::Lanczos3)
    };
    let mut buffer = Cursor::new(Vec::new());
    resized.write_to(&mut buffer, ImageFormat::WebP)?;
    fs::write(target, buffer.into_inner())?;
    Ok(())
}

fn print_json<T: Serialize>(value: &T) -> Result<()> {
    println!("{}", serde_json::to_string(value)?);
    Ok(())
}
