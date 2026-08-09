fn main() {
    if let Err(error) = slint_build::compile("ui/app.slint") {
        panic!("the checked-in Slint UI must compile: {error}");
    }
}
