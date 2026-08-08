//! Platform-neutral video frame conversion for native capture backends.
//!
//! Capture APIs commonly return packed BGRA. `LiveKit` consumes planar I420.
//! Keeping the conversion here makes the native screen and camera backends
//! replaceable without coupling either one to the room transport.

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct I420Frame {
    pub width: u32,
    pub height: u32,
    pub y: Vec<u8>,
    pub u: Vec<u8>,
    pub v: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PackedPixelFormat {
    Bgra,
    Rgba,
    Rgb,
}

#[derive(Clone, Copy, Debug)]
pub struct PackedFrame<'a> {
    pub width: u32,
    pub height: u32,
    pub stride: usize,
    pub format: PackedPixelFormat,
    pub data: &'a [u8],
}

impl PackedFrame<'_> {
    #[must_use]
    pub fn to_i420(self) -> Option<I420Frame> {
        let bytes_per_pixel = match self.format {
            PackedPixelFormat::Bgra | PackedPixelFormat::Rgba => 4,
            PackedPixelFormat::Rgb => 3,
        };
        let width = usize::try_from(self.width).ok()?;
        let height = usize::try_from(self.height).ok()?;
        if width == 0
            || height == 0
            || self.stride < width.checked_mul(bytes_per_pixel)?
            || self.data.len() < self.stride.checked_mul(height)?
        {
            return None;
        }

        let chroma_width = width.div_ceil(2);
        let chroma_height = height.div_ceil(2);
        let mut y = vec![0_u8; width.checked_mul(height)?];
        let mut u = vec![0_u8; chroma_width.checked_mul(chroma_height)?];
        let mut v = vec![0_u8; chroma_width.checked_mul(chroma_height)?];

        for row in 0..height {
            for column in 0..width {
                let (red, green, blue) = self.rgb_at(row, column, bytes_per_pixel);
                y[row * width + column] = luma(red, green, blue);
            }
        }

        for chroma_row in 0..chroma_height {
            for chroma_column in 0..chroma_width {
                let mut red = 0_u32;
                let mut green = 0_u32;
                let mut blue = 0_u32;
                let mut samples = 0_u32;
                for dy in 0..2 {
                    for dx in 0..2 {
                        let row = chroma_row * 2 + dy;
                        let column = chroma_column * 2 + dx;
                        if row < height && column < width {
                            let pixel = self.rgb_at(row, column, bytes_per_pixel);
                            red += u32::from(pixel.0);
                            green += u32::from(pixel.1);
                            blue += u32::from(pixel.2);
                            samples += 1;
                        }
                    }
                }
                let index = chroma_row * chroma_width + chroma_column;
                u[index] = chroma_u(
                    u8::try_from(red / samples).unwrap_or(u8::MAX),
                    u8::try_from(green / samples).unwrap_or(u8::MAX),
                    u8::try_from(blue / samples).unwrap_or(u8::MAX),
                );
                v[index] = chroma_v(
                    u8::try_from(red / samples).unwrap_or(u8::MAX),
                    u8::try_from(green / samples).unwrap_or(u8::MAX),
                    u8::try_from(blue / samples).unwrap_or(u8::MAX),
                );
            }
        }

        Some(I420Frame {
            width: self.width,
            height: self.height,
            y,
            u,
            v,
        })
    }

    fn rgb_at(&self, row: usize, column: usize, bytes_per_pixel: usize) -> (u8, u8, u8) {
        let offset = row * self.stride + column * bytes_per_pixel;
        match self.format {
            PackedPixelFormat::Bgra => (
                self.data[offset + 2],
                self.data[offset + 1],
                self.data[offset],
            ),
            PackedPixelFormat::Rgba | PackedPixelFormat::Rgb => (
                self.data[offset],
                self.data[offset + 1],
                self.data[offset + 2],
            ),
        }
    }
}

fn clamp(value: i32) -> u8 {
    u8::try_from(value.clamp(0, 255)).unwrap_or(u8::MAX)
}

// Full-range BT.601 is appropriate for RGB screen/camera sources and avoids
// crushing desktop whites/blacks during a packed RGB -> WebRTC I420 roundtrip.
fn luma(red: u8, green: u8, blue: u8) -> u8 {
    clamp((77 * i32::from(red) + 150 * i32::from(green) + 29 * i32::from(blue) + 128) >> 8)
}

fn chroma_u(red: u8, green: u8, blue: u8) -> u8 {
    clamp(((-43 * i32::from(red) - 85 * i32::from(green) + 128 * i32::from(blue) + 128) >> 8) + 128)
}

fn chroma_v(red: u8, green: u8, blue: u8) -> u8 {
    clamp(((128 * i32::from(red) - 107 * i32::from(green) - 21 * i32::from(blue) + 128) >> 8) + 128)
}

#[cfg(test)]
mod tests {
    use super::{PackedFrame, PackedPixelFormat};

    #[test]
    fn converts_bgra_with_odd_dimensions_and_padding() {
        let bytes = [
            0, 0, 255, 255, 0, 255, 0, 255, 9, 9, 9, 9, // red, green, padding
            255, 0, 0, 255, 255, 255, 255, 255, 9, 9, 9, 9, // blue, white, padding
            0, 0, 0, 255, 128, 128, 128, 255, 9, 9, 9, 9, // black, gray, padding
        ];
        let converted = PackedFrame {
            width: 2,
            height: 3,
            stride: 12,
            format: PackedPixelFormat::Bgra,
            data: &bytes,
        }
        .to_i420();
        let Some(frame) = converted else {
            panic!("valid frame was rejected");
        };
        assert_eq!(frame.y.len(), 6);
        assert_eq!(frame.u.len(), 2);
        assert_eq!(frame.v.len(), 2);
        assert!(frame.y[0] > frame.y[2]);
        assert_eq!(frame.y[4], 0);
    }

    #[test]
    fn rejects_short_or_invalid_frames() {
        assert!(
            PackedFrame {
                width: 4,
                height: 4,
                stride: 3,
                format: PackedPixelFormat::Rgb,
                data: &[0; 64],
            }
            .to_i420()
            .is_none()
        );
    }
}
