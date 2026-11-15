import sounddevice as sd


def main() -> None:
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        name = d.get("name", "")
        ins = d.get("max_input_channels", 0)
        outs = d.get("max_output_channels", 0)
        default_sr = d.get("default_samplerate", "")
        print(f"{i:>3}  in={ins:<2} out={outs:<2} sr={default_sr:<7}  {name}")


if __name__ == "__main__":
    main()

